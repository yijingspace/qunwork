"""Real-time voice chat pipeline: VAD → streaming ASR → LLM → TTS.

The loop (run in a background thread so the API stays responsive):

1. The microphone is captured in 16 kHz mono float32 chunks.
2. A silero VAD (via sherpa-onnx) marks speech segments; each segment's audio
   is fed to a streaming Zipformer recognizer that emits a live partial text.
3. When the segment ends, the final transcript is handed to the injected LLM
   callable (`complete(messages) -> str`).
4. The reply is synthesized with the local VITS voice and played back.

State and events are pushed to the host through callbacks; the host (the
FastAPI app) buffers them for the GUI to poll via /v1/voice/events.

Audio never leaves the device and nothing is persisted.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from coworker.voice.models import voice_model_dir, voice_models_installed

SAMPLE_RATE = 16_000
_CHUNK_SECONDS = 0.1  # VAD/ASR step size
_CHUNK_SAMPLES = int(SAMPLE_RATE * _CHUNK_SECONDS)

# A complete() callable: takes a list of {"role","content"} messages, returns text.
CompleteFn = Callable[[list[dict]], str]


@dataclass
class VoiceEvent:
    """One event emitted by the pipeline (appended to the host's event buffer)."""

    type: str  # "state" | "partial" | "final" | "reply" | "error"
    payload: str = ""
    detail: str = ""
    ts: float = field(default_factory=lambda: __import__("time").time())


class VoiceChatPipeline:
    """A single microphone session with its own worker thread.

    Usage:
        pipe = VoiceChatPipeline(complete=my_llm)
        pipe.start()
        pipe.stop()          # blocks until the worker exits
        events = pipe.drain_events()
    """

    def __init__(
        self,
        complete: Optional[CompleteFn] = None,
        on_event: Optional[Callable[[VoiceEvent], None]] = None,
    ):
        self.complete = complete
        self.on_event = on_event
        self._events: list[VoiceEvent] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False

    # -- event buffer ---------------------------------------------------------
    def _emit(self, ev: VoiceEvent) -> None:
        with self._lock:
            self._events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def drain_events(self, after: int = 0) -> list[dict]:
        """Return events with index > `after` as plain dicts, with their indices."""
        with self._lock:
            out = []
            for i in range(after, len(self._events)):
                ev = self._events[i]
                out.append({"index": i, "type": ev.type, "payload": ev.payload, "detail": ev.detail})
            return out

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        if not voice_models_installed():
            raise RuntimeError("voice models are not installed")
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._run, name="voice-chat", daemon=True
        )
        self._thread.start()
        self._emit(VoiceEvent("state", "running"))

    def stop(self, join: bool = True) -> None:
        if not self.running:
            return
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=10)
        self.running = False
        # The worker's finally block emits the "stopped" state; don't double-emit here.

    # -- the worker loop ------------------------------------------------------
    def _run(self) -> None:
        try:
            import sounddevice as sd
            import sherpa_onnx

            model_dir = voice_model_dir()
            # VAD
            vad_config = sherpa_onnx.VadModelConfig()
            vad_config.sample_rate = SAMPLE_RATE
            vad_config.num_threads = 2
            vad_config.silero_vad = sherpa_onnx.SileroVadModelConfig()
            vad_config.silero_vad.model = str(model_dir / "silero_vad.onnx")
            vad_config.silero_vad.threshold = 0.5
            vad_config.silero_vad.min_silence_duration = 0.5
            vad_config.silero_vad.min_speech_duration = 0.25
            vad_config.silero_vad.max_speech_duration = 30.0
            vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=60)

            # Streaming ASR (Zipformer transducer)
            zf = model_dir / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
            recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(zf / "tokens.txt"),
                encoder=str(zf / "encoder-epoch-99-avg-1.int8.onnx"),
                decoder=str(zf / "decoder-epoch-99-avg-1.onnx"),
                joiner=str(zf / "joiner-epoch-99-avg-1.int8.onnx"),
                num_threads=2,
                decoding_method="greedy_search",
                enable_endpoint_detection=True,
            )

            # TTS (VITS zh) — `dict_dir` carries the jieba segmentation dictionary;
            # without it the synthesizer degrades to per-character output and the
            # spoken audio is noticeably worse.
            vits = model_dir / "sherpa-onnx-vits-zh-ll"
            tts_config = sherpa_onnx.OfflineTtsConfig()
            tts_config.model = sherpa_onnx.OfflineTtsModelConfig()
            tts_config.model.vits = sherpa_onnx.OfflineTtsVitsModelConfig()
            tts_config.model.vits.model = str(vits / "model.onnx")
            tts_config.model.vits.tokens = str(vits / "tokens.txt")
            tts_config.model.vits.lexicon = str(vits / "lexicon.txt")
            tts_config.model.vits.dict_dir = str(vits / "dict")
            tts_config.model.num_threads = 2
            tts = sherpa_onnx.OfflineTts(tts_config)

            self._emit(VoiceEvent("state", "ready"))

            # -- capture loop -------------------------------------------------
            def callback(indata, frames, time_info, status):
                if self._stop.is_set():
                    return
                chunk = np.asarray(indata[:, 0], dtype=np.float32)
                # VAD config carries the sample rate; accept_waveform takes samples only.
                vad.accept_waveform(chunk)

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=_CHUNK_SAMPLES,
                callback=callback,
            ):
                stream = recognizer.create_stream()
                while not self._stop.is_set():
                    if not vad.empty():
                        seg = vad.front
                        # Feed the full segment's samples into the ASR stream.
                        samples = seg.samples
                        if len(samples) > 0:
                            stream.accept_waveform(SAMPLE_RATE, samples)
                            while recognizer.is_ready(stream):
                                recognizer.decode_stream(stream)
                        vad.pop()
                        text = recognizer.get_result(stream)
                        stream = recognizer.create_stream()  # fresh stream per segment
                        text = (text or "").strip()
                        if text:
                            self._emit(VoiceEvent("partial", text))
                            self._handle_turn(text, tts)
                    else:
                        self._stop.wait(_CHUNK_SECONDS)
            # -- end of with block (mic closed) --------------------------------
        except Exception as exc:  # noqa: BLE001 — surface to the host/GUI
            self._emit(VoiceEvent("error", str(exc)))
        finally:
            self.running = False
            self._emit(VoiceEvent("state", "stopped"))

    def _handle_turn(self, text: str, tts) -> None:
        """Send the transcript to the LLM, then speak the reply."""
        if not self.complete:
            self._emit(VoiceEvent("error", "no LLM callable configured"))
            return
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是 QunWork 的本地语音助手。请用简洁、自然的口语回答用户，"
                        "直接给出答案，不要使用任何 Markdown 标记或列表符号。"
                    ),
                },
                {"role": "user", "content": text},
            ]
            reply = (self.complete(messages) or "").strip()
        except Exception as exc:  # noqa: BLE001
            self._emit(VoiceEvent("error", f"LLM failed: {exc}"))
            return
        self._emit(VoiceEvent("reply", reply))
        try:
            audio = tts.generate(reply, sid=0, speed=1.0)
            samples = getattr(audio, "samples", None)
            sr = getattr(audio, "sample_rate", None) or SAMPLE_RATE
            if samples is not None and len(samples) > 0:
                import sounddevice as sd

                sd.play(samples, sr)
                sd.wait()
        except Exception as exc:  # noqa: BLE001
            self._emit(VoiceEvent("error", f"TTS playback failed: {exc}"))
