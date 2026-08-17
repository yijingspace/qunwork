"""Local real-time voice chat pipeline (VAD + streaming ASR + LLM + TTS).

This is the "layered pipeline" voice assistant: silero VAD detects speech
segments on the mic, a streaming Zipformer ASR transcribes them, the result is
sent to the configured QunWork LLM, and the reply is synthesized locally with a
VITS voice model. Everything runs on-device; no audio ever leaves the machine.

All heavy imports (sherpa_onnx, sounddevice) are lazy so that the rest of the
sidecar keeps working on machines where the voice extras are not installed.
"""

from coworker.voice.models import (
    VOICE_MODELS,
    VoiceModel,
    ensure_voice_models,
    install_voice_models,
    voice_model_dir,
    voice_models_installed,
    voice_models_status,
)

__all__ = [
    "VOICE_MODELS",
    "VoiceModel",
    "ensure_voice_models",
    "install_voice_models",
    "voice_model_dir",
    "voice_models_installed",
    "voice_models_status",
]
