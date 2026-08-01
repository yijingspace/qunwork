# QunWork AI assistant bootstrap

This file is a workspace-level guide for Copilot Chat and other AI assistants working in the QunWork repository.

## What this repo is
- `coworker/` is the Python backend: the agent engine, model providers, connectors, MCP client, automations, and desktop server.
- `surfaces/gui/` is the desktop UI and shell: React + Vite + Tauri.
- `stt/` contains a Rust speech-to-text sidecar.
- `packaging/` contains packaging scripts, installer build helpers, and the dev bootstrap script.
- `tests/` contains the backend Python test suite.
- `docs/` contains design docs and implementation notes.

## Primary workflows
- Backend development: edit `coworker/`, update tests under `tests/`, and use the Python virtual environment created at `.venv`.
- UI development: edit `surfaces/gui/`, use `npm`/`npm run`, and follow the UI README in `surfaces/gui/README.md`.
- Packaging and release tooling: edit `packaging/` only when changing build or installer behavior.

## Recommended entry points
- `README.md` for high-level architecture, dev setup, and test commands.
- `pyproject.toml` for dependencies, package metadata, and entry-point scripts.
- `packaging/setup_dev_env.sh` for local Python bootstrap instructions.
- `coworker/agent.py` for agent engine build logic.
- `coworker/server.py` for server startup and API routing.
- `surfaces/gui/` for frontend and Tauri shell behavior.

## Dev setup and run commands
- One-time bootstrap: `bash packaging/setup_dev_env.sh`
- Start backend server: `.venv/bin/qunwork-server --cwd /path/to/project --port 8765`
  - On Windows, use `.venv\Scripts\qunwork-server.exe`.
- Start browser UI: `cd surfaces/gui && npm install && npm run dev`
- Start desktop shell: `cd surfaces/gui && npm run tauri dev`

## Test commands
- Backend tests: `.venv/bin/pytest`
- UI tests: `cd surfaces/gui && npm test`
- GUI end-to-end: `cd surfaces/gui && npm run e2e`

## Project conventions and notes
- The Python package root is `coworker`; all backend modules are imported from there.
- `pyproject.toml` pins `aisuite` from a Git commit and uses `setuptools` packaging.
- The repo is agent-first: connector changes, model provider updates, and tool integrations are central.
- The desktop app is local-first; changes should preserve local security, approval gating, and user-controlled connector behavior.

## When adding AI or automation fixes
- Prefer backend changes in `coworker/` and tests in `tests/` unless behavior is purely UI.
- Keep frontend work isolated to `surfaces/gui/` and do not mix backend API changes with UI-only fixes.
- Do not duplicate full setup or build instructions; link to `README.md` and `surfaces/gui/README.md` instead.

## Search hints
- Look for `qunwork-server`, `qunwork`, `coworker.agent`, `coworker.server`, and `packaging/setup_dev_env.sh`.
- For UI surface work, search `surfaces/gui/` and `README.md` in that folder.
- For packaging and install/build work, search `packaging/build_dmg.sh` and `packaging/build_windows.ps1`.

## Avoid
- Avoid assuming this is a pure Python repo; the UI and packaging layers are separate.
- Avoid changing build scripts or packaging without explicit reason.
- Avoid using unsupported platform commands; prefer the documented shell workflows.
