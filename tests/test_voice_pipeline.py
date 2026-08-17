"""Tests for the local voice pipeline — model provisioning + event plumbing.

The heavy runtime (sherpa_onnx, sounddevice, real mic) is not exercised here:
model *installation* is tested with a tiny fake archive via the public API, and
the pipeline's event/drain/stop logic is tested with a stub class so the test
suite stays hermetic and fast on any machine (CI has no audio devices).
"""

from __future__ import annotations

import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

# Ensure the venv can import coworker.voice before models exist on disk.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point state_dir() at a temp dir so tests never touch the real model cache."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    yield tmp_path


def _make_fake_archive(path: Path, members: dict[str, bytes], top: str) -> None:
    """Write a tar.bz2 archive whose members are nested under `top/`."""
    with tarfile.open(path, "w:bz2") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, __import__("io").BytesIO(data))


def test_voice_models_status_empty_before_install():
    from coworker.voice import voice_models_status

    status = voice_models_status()
    assert set(status) == {"vad", "asr", "tts"}
    assert all(not m["installed"] for m in status.values())


def test_install_plain_file_model_vad(tmp_path, monkeypatch):
    """The VAD model is a plain .onnx file — download+verify+marker path."""
    from coworker.voice import models as vm

    vad = vm.VOICE_MODELS["vad"]
    # Pretend the file is already on disk (size matches) → verify + marker, no download.
    dest = vm.voice_model_dir() / "silero_vad.onnx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\x00" * vad.size)
    vm._install_one(vad, None)
    assert (vm.voice_model_dir() / "vad.ok").is_file()
    assert vm.voice_models_status()["vad"]["installed"]


def test_install_tar_model_asr(tmp_path, monkeypatch):
    """The ASR model is a tar.bz2 — unpacked dir with required files gets a marker."""
    from coworker.voice import models as vm

    asr = vm.VOICE_MODELS["asr"]
    d = vm.voice_model_dir()
    d.mkdir(parents=True, exist_ok=True)
    arc = d / f"{asr.unpack_dir}.tar.bz2"
    # Write an archive that only carries the required files (size check is bypassed
    # because we create the archive manually — set the file to expected size first).
    payload = b"x" * 64
    _make_fake_archive(
        arc,
        {f: payload for f in asr.required_files},
        asr.unpack_dir,
    )
    # Patch size verification: our fake archive is small; simulate by stubbing the
    # size check to accept it (the real download path is covered by the plain-file test).
    monkeypatch.setattr(vm, "_verify_file", lambda path, model: None)
    vm._install_one(asr, None)
    target = d / asr.unpack_dir
    for f in asr.required_files:
        assert (target / f).is_file(), f"missing {f}"
    assert (d / "asr.ok").is_file()
    assert vm.voice_models_status()["asr"]["installed"]


def test_install_idempotent_when_marked(tmp_path, monkeypatch):
    """A .ok marker makes re-install a no-op (no download attempt)."""
    from coworker.voice import models as vm

    for key, model in vm.VOICE_MODELS.items():
        (vm.voice_model_dir() / f"{key}.ok").write_text("ok\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(vm, "_download", lambda url, dest, progress: calls.append(url))
    vm.install_voice_models()
    assert calls == []


def test_voice_models_installed_requires_all():
    from coworker.voice import models as vm

    assert not vm.voice_models_installed()
    for key in vm.VOICE_MODELS:
        (vm.voice_model_dir() / f"{key}.ok").write_text("ok\n", encoding="utf-8")
    assert vm.voice_models_installed()


class _FakeTts:
    """Stands in for sherpa_onnx.OfflineTts in the pipeline's LLM-turn path."""

    def generate(self, text, sid=0, speed=1.0):
        return None, 16000  # samples=None → playback skipped


class _FakePipelineDeps:
    """Patches the modules the pipeline imports at runtime with inert stubs."""

    def __enter__(self):
        import importlib

        self._orig = {}

        class _FakeSherpa:
            class VadModelConfig:
                pass

            class SileroVadModelConfig:
                pass

            class VoiceActivityDetector:
                def __init__(self, config, buffer_size_in_seconds=60):
                    self.segments = []

                def accept_waveform(self, sr, chunk):
                    pass

                def empty(self):
                    return True

                def front(self):
                    raise RuntimeError("no segments in stub")

                def pop(self):
                    pass

            class OnlineRecognizer:
                @staticmethod
                def from_transducer(**kwargs):
                    return _FakeRecognizer()

            class OfflineTtsConfig:
                pass

            class OfflineTtsModelConfig:
                pass

            class OfflineTtsVitsModelConfig:
                pass

        class _FakeRecognizer:
            def create_stream(self):
                return object()

            def is_ready(self, stream):
                return False

            def get_result(self, stream):
                return ""

        class _FakeSounddevice:
            def InputStream(self, **kwargs):
                raise RuntimeError("mic unavailable in tests")

            def play(self, samples, sr):
                pass

            def wait(self):
                pass

        sys.modules["sherpa_onnx"] = _FakeSherpa()
        sys.modules["sounddevice"] = _FakeSounddevice()
        return self

    def __exit__(self, *exc):
        for name in ("sherpa_onnx", "sounddevice"):
            sys.modules.pop(name, None)
        return False


def test_pipeline_drain_events_and_start_requires_models(tmp_path, monkeypatch):
    from coworker.voice import pipeline as pl

    # No models installed → start must refuse.
    pipe = pl.VoiceChatPipeline(complete=lambda messages: "ok")
    with pytest.raises(RuntimeError):
        pipe.start()

    # Install markers so the pipeline passes the guard, then stub the runtime.
    from coworker.voice import models as vm

    for key in vm.VOICE_MODELS:
        (vm.voice_model_dir() / f"{key}.ok").write_text("ok\n", encoding="utf-8")

    with _FakePipelineDeps():
        pipe = pl.VoiceChatPipeline(complete=lambda messages: "ok")
        pipe.start()
        # The worker thread should quickly hit the (stubbed) mic error and stop.
        pipe.stop(join=True)
        assert not pipe.running
        types = [e["type"] for e in pipe.drain_events()]
        assert "state" in types


def test_pipeline_llm_turn_with_tts_failure(tmp_path, monkeypatch):
    """A TTS playback error must surface as an event, not kill the pipeline."""
    from coworker.voice import pipeline as pl
    from coworker.voice import models as vm

    for key in vm.VOICE_MODELS:
        (vm.voice_model_dir() / f"{key}.ok").write_text("ok\n", encoding="utf-8")

    with _FakePipelineDeps():
        pipe = pl.VoiceChatPipeline(complete=lambda messages: "你好，这是回复。")
        # Drive _handle_turn directly with a tts stub that raises on generate.
        class _BadTts:
            def generate(self, *a, **k):
                raise RuntimeError("audio device busy")

        pipe._handle_turn("你好", _BadTts())
        events = pipe.drain_events()
        assert any(e["type"] == "reply" for e in events)
        assert any(e["type"] == "error" and "TTS" in e["payload"] for e in events)


def test_pipeline_complete_failure_surfaces_event(tmp_path):
    from coworker.voice import pipeline as pl

    def boom(messages):
        raise RuntimeError("provider down")

    pipe = pl.VoiceChatPipeline(complete=boom)
    pipe._handle_turn("测试", _FakeTts())
    events = pipe.drain_events()
    assert any(e["type"] == "error" and "LLM" in e["payload"] for e in events)
