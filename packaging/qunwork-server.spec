# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the bundled `qunwork-server` (desktop sidecar).

One-DIR bundle (exe + `_internal/` support folder) shipped via Tauri's `resources` slot.
It used to be a onefile binary in the externalBin slot, but onefile self-extracts its whole
archive to a temp dir on EVERY launch — 6-7s of "Starting coworker…" splash (measured; the
actual Python import is ~0.5s). The wrinkles handled here:
  - aisuite is a regular pip dependency (git-pinned in pyproject.toml); collect coworker +
    aisuite submodules from the venv.
  - uvicorn loads its protocol/lifespan impls dynamically → collect_all.
  - certifi's CA bundle must ship for TLS (OpenAI, web search, Telegram/Slack).
  - messaging extras (slack_bolt, telegram) are optional; collected if importable.

Cross-platform: paths are derived from this spec's own location (SPECPATH), never hardcoded,
so the same spec builds native binaries on macOS, Windows, and Linux. On Windows PyInstaller
appends `.exe` to `name`. The binary is built as a normal console app on every OS — a windowed
(console=False) build leaves sys.stdout/stderr as None, which breaks uvicorn's startup logging
and hangs the server. To avoid a console window flashing in the desktop app, the Tauri shell
spawns this sidecar with the Windows CREATE_NO_WINDOW flag (see src-tauri/src/lib.rs), which
hides the window while keeping stdio intact.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller and points at this file's directory
# (<repo>/packaging). Derive everything else from it — no hardcoded paths.
PACKAGING = SPECPATH
ROOT = os.path.dirname(PACKAGING)

IS_WINDOWS = sys.platform == "win32"

# Experimental (use-at-your-own-risk) connectors are excluded from official builds: the code
# is stripped, not just disabled. Self-builders opt in with COWORKER_EXPERIMENTAL=1; the
# loader in coworker/connectors/descriptors.py treats the missing package as a no-op.
INCLUDE_EXPERIMENTAL = os.environ.get("COWORKER_EXPERIMENTAL") == "1"

hiddenimports = []
datas = []
binaries = []

for pkg in ("coworker", "aisuite", "mcp", "ddgs", "croniter", "docstring_parser"):
    hiddenimports += collect_submodules(pkg)
if not INCLUDE_EXPERIMENTAL:
    hiddenimports = [
        m for m in hiddenimports if not m.startswith("coworker.connectors.experimental")
    ]

# `websockets` powers the managed Slack relay client (relay_client.py). It is
# lazy-imported inside a function, so PyInstaller's static analysis misses it —
# collect it explicitly or the packaged relay adapter fails to open its socket.
# `pypdf`/`pypdfium2` are lazy-imported the same way (pdf_support.py) — and pypdfium2
# carries the libpdfium binary, which collect_all is what actually stages.
for pkg in ("uvicorn", "certifi", "anyio", "websockets", "pypdf", "pypdfium2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# `rapidocr_onnxruntime` powers the vision skill's local OCR. It is imported
# lazily inside vision_analyze.py, so static analysis misses it — and its ONNX
# models ship as package data, so it needs collect_all (not just submodules) or
# the packaged sidecar degrades to metadata-only image analysis.
for pkg in ("rapidocr_onnxruntime", "onnxruntime"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# `sherpa_onnx` (local voice chat: VAD + streaming ASR + TTS) and `sounddevice`
# (mic capture / playback) are lazy-imported inside coworker/voice/pipeline.py, so
# static analysis misses them. sherpa-onnx ships its native core + onnxruntime
# bindings as package data → collect_all. sounddevice also needs its PortAudio
# backend data files.
for pkg in ("sherpa_onnx", "sounddevice"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Windows has no system tz database; tzdata ships the zoneinfo files the scheduler needs.
if IS_WINDOWS:
    try:
        d, b, h = collect_all("tzdata")
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Built-in skills (coworker/skills/*): collect_submodules only pulls importable .py
# modules into the PYZ — SKILL.md bodies, grimoire/ docs and forge/ CLI scripts are
# DATA, not modules, and would silently vanish from the packaged sidecar (vision's
# SKILL.md, and img2threejs' whole forge/ + grimoire/ tree, need to be on disk).
# Stage the whole tree as data so the loader's built-in (read-only) dir works identically
# in dev and in the shipped app. The destination keeps the `coworker/skills/...` layout
# that SkillLoader._dirs resolves against (…/coworker/skills).
_SKILLS_SRC = os.path.join(ROOT, "coworker", "skills")
if os.path.isdir(_SKILLS_SRC):
    datas.append((_SKILLS_SRC, os.path.join("coworker", "skills")))

for pkg in ("slack_bolt", "telegram"):  # [messaging] extra — optional
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(PACKAGING, "server_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "PyQt5", "PySide6"]
    + ([] if INCLUDE_EXPERIMENTAL else ["coworker.connectors.experimental"]),
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="qunwork-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Console on every OS: a windowed build nulls stdout/stderr and hangs uvicorn. The Tauri
    # shell hides the window on Windows via CREATE_NO_WINDOW when spawning the sidecar.
    console=True,
    # target_arch left unset → PyInstaller builds for the host architecture.
)
# Onedir: dist/qunwork-server/{qunwork-server[.exe], _internal/}. The build scripts stage
# this whole folder into src-tauri/binaries/sidecar/ for Tauri's `resources` bundling.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="qunwork-server",
)
