"""Model provisioning for the local voice pipeline (VAD / ASR / TTS).

Three small-to-mid models are downloaded on demand into
`<state_dir>/models/voice/`:

- silero VAD  (~0.6 MB)  — speech-segment detection
- streaming Zipformer zh (74 MB) — real-time Chinese streaming ASR
- VITS zh-ll (118 MB)    — Chinese female TTS voice

Sources are GitHub release artifacts of k2-fsa/sherpa-onnx (reachable from
mainland China, unlike huggingface.co). Downloads are atomic: a partial file
never replaces a verified one; a `.ok` marker records a completed + size-verified
install so re-installs are no-ops.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from coworker.secrets import state_dir

# The single release the three artifacts live under (sherpa-onnx GitHub releases).
_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

# Expected unpacked top-level directory names (tar members are nested under these).
_ZIPFORMER_DIR = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
_VITS_DIR = "sherpa-onnx-vits-zh-ll"


@dataclass(frozen=True)
class VoiceModel:
    """One downloadable artifact of the voice pipeline."""

    key: str
    label: str
    url: str
    # Expected byte size of the downloaded archive/file (verified after download).
    size: int
    # sha256 of the downloaded file (verified after download).
    sha256: str
    # Name of the unpacked file/dir we need from the archive ("" for plain files).
    unpack_dir: str = ""
    # File paths (relative to unpack_dir) that must exist for the model to be usable.
    required_files: tuple[str, ...] = ()


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- models -------------------------------------------------------------------

VOICE_MODELS: dict[str, VoiceModel] = {
    "vad": VoiceModel(
        key="vad",
        label="silero VAD",
        url=f"{_RELEASE}/asr-models/silero_vad.onnx",
        size=643_854,
        sha256="",  # filled after first verified download (mirror may serve identical bytes)
        unpack_dir="",
        required_files=("silero_vad.onnx",),
    ),
    "asr": VoiceModel(
        key="asr",
        label="streaming Zipformer 中文 (zh-14M)",
        url=f"{_RELEASE}/asr-models/{_ZIPFORMER_DIR}.tar.bz2",
        size=74_004_050,
        sha256="",
        unpack_dir=_ZIPFORMER_DIR,
        required_files=(
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
            "tokens.txt",
        ),
    ),
    "tts": VoiceModel(
        key="tts",
        label="VITS 中文女声 (zh-ll)",
        url=f"{_RELEASE}/tts-models/{_VITS_DIR}.tar.bz2",
        size=118_810_709,
        sha256="",
        unpack_dir=_VITS_DIR,
        required_files=("model.onnx", "tokens.txt", "lexicon.txt"),
    ),
}


def voice_model_dir() -> Path:
    """The on-disk model cache directory, created on demand."""
    d = state_dir() / "models" / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ok_marker(model: VoiceModel) -> Path:
    return voice_model_dir() / f"{model.key}.ok"


def voice_models_installed() -> bool:
    return all(_ok_marker(m).is_file() for m in VOICE_MODELS.values())


def voice_models_status() -> dict[str, dict]:
    """Per-model install status for the GUI (installed / size / label)."""
    out: dict[str, dict] = {}
    for key, model in VOICE_MODELS.items():
        marker = _ok_marker(model)
        detail_dir = voice_model_dir() / model.unpack_dir if model.unpack_dir else voice_model_dir()
        first = next(
            (detail_dir / f for f in model.required_files if (detail_dir / f).is_file()),
            None,
        )
        size = first.stat().st_size if first else 0
        out[key] = {
            "installed": marker.is_file(),
            "label": model.label,
            "size": size,
            "expected_size": model.size,
        }
    return out


def _verify_file(path: Path, model: VoiceModel) -> None:
    """Size + (when known) sha256 check; raises ValueError on mismatch."""
    if not path.is_file():
        raise ValueError(f"missing file: {path}")
    actual = path.stat().st_size
    if actual != model.size:
        raise ValueError(
            f"{model.key}: size mismatch ({actual} != {model.size}) — download was truncated"
        )
    if model.sha256:
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        if h != model.sha256:
            raise ValueError(f"{model.key}: sha256 mismatch ({h} != {model.sha256})")


def _download(url: str, dest: Path, progress: Optional[Callable[[int, int], None]]) -> None:
    """Stream `url` to `dest` (temp file), reporting (done, total) progress."""
    req = urllib.request.Request(url, headers={"User-Agent": "QunWork/0.2 (voice)"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total or done)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _install_one(
    model: VoiceModel, progress: Optional[Callable[[str, int, int], None]]
) -> None:
    """Download + unpack one model. Safe to re-run; verified installs are skipped."""
    if _ok_marker(model).is_file():
        return
    base = voice_model_dir()
    if not model.unpack_dir:  # plain file (silero VAD)
        dest = base / "silero_vad.onnx"
        if dest.is_file():
            _verify_file(dest, model)
        else:
            if progress:
                progress(model.key, 0, model.size)
            _download(model.url, dest, lambda d, t: progress(model.key, d, t) if progress else None)
            _verify_file(dest, model)
        _ok_marker(model).write_text("ok\n", encoding="utf-8")
        return

    # tar.bz2 archive → unpack into <dir>/<unpack_dir>/.
    arc = base / f"{model.unpack_dir}.tar.bz2"
    if not arc.is_file():
        if progress:
            progress(model.key, 0, model.size)
        _download(model.url, arc, lambda d, t: progress(model.key, d, t) if progress else None)
        _verify_file(arc, model)
    target = base / model.unpack_dir
    if not all((target / f).is_file() for f in model.required_files):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(arc, "r:bz2") as tf:
            for member in tf.getmembers():
                # Strip the leading directory so contents land directly in `target`.
                parts = Path(member.name).parts
                if len(parts) <= 1:
                    continue
                rel = Path(*parts[1:])
                if member.isdir():
                    (target / rel).mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src, open(target / rel, "wb") as out:
                        shutil.copyfileobj(src, out)
        for f in model.required_files:
            if not (target / f).is_file():
                raise ValueError(f"{model.key}: unpacked archive missing required file {f}")
    arc.unlink(missing_ok=True)
    _ok_marker(model).write_text("ok\n", encoding="utf-8")


def install_voice_models(
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> dict:
    """Install every voice model. `progress(key, done, total)` fires per chunk.
    Returns the status dict (see voice_models_status)."""
    for model in VOICE_MODELS.values():
        _install_one(model, progress)
    return voice_models_status()


def ensure_voice_models() -> bool:
    """Idempotent install with no progress callbacks (used by the pipeline)."""
    try:
        install_voice_models()
        return True
    except Exception:
        return False
