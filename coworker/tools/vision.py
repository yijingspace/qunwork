"""analyze_image — OCR + metadata for an uploaded image, for text-only models.

The model receives `[image: <path>]` in context (see build_user_content) but has no
vision. This tool lets it actually *read* the picture: local RapidOCR text extraction +
PIL metadata (+ optional VL API semantics when configured). It is a low-risk READ tool
and is auto-approved — otherwise every image+text message would stall on a
permission prompt the user never expects ("LLM 没有反应", owner bug 2026-08-18).

The analyzer script ships inside the app bundle (coworker/skills/vision/resources/
vision_analyze.py), so we load it by file path rather than a package import.
"""

from __future__ import annotations

import json
from pathlib import Path

import aisuite as ai

_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": (
            "Analyze an image file by path: extract OCR text (RapidOCR, local), read "
            "metadata (size/format), and optionally run a vision-language API if one is "
            "configured. Use when the user uploaded an image ([image: <path>] in context) "
            "and you need to see what is in it — you have no vision, so this is the only "
            "way to actually read the picture."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the image file (from the [image: ...] marker).",
                },
                "ocr_only": {
                    "type": "boolean",
                    "description": "Skip the VL API and return OCR + metadata only (faster, fully local).",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
}


def _load_analyzer():
    """Import the bundled vision_analyze.py by file path (works in PyInstaller bundles too)."""
    here = Path(__file__).resolve().parent.parent  # coworker/
    script = here / "skills" / "vision" / "resources" / "vision_analyze.py"
    if not script.is_file():
        raise FileNotFoundError(f"vision_analyze.py not found at {script}")
    import importlib.util

    spec = importlib.util.spec_from_file_location("_coworker_vision_analyze", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _analyze_image(path: str, ocr_only: bool = False) -> str:
    p = Path(path)
    if not p.is_file():
        return json.dumps({"error": f"file not found: {path}"}, ensure_ascii=False)
    try:
        mod = _load_analyzer()
        meta = mod._metadata(p)
        ocr, ocr_warning = mod._ocr(p)
        result: dict = {"path": str(p), "metadata": meta, "ocr": ocr}
        if ocr_warning:
            result["ocr_warning"] = ocr_warning
        if not ocr_only:
            vision, vision_warning = mod._vision(p)
            result["vision"] = vision
            if vision_warning:
                result["vision_warning"] = vision_warning
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:  # fail soft: report the error, never crash the turn
        return json.dumps(
            {"error": f"{type(exc).__name__}: {exc}", "path": str(p)},
            ensure_ascii=False,
        )


def vision_tools() -> list:
    """The image-analysis toolset (single tool, low-risk READ, auto-approved)."""

    def analyze_image(path: str, ocr_only: bool = False) -> str:
        return _analyze_image(path, ocr_only=bool(ocr_only))

    analyze_image.__name__ = "analyze_image"
    analyze_image.__doc__ = _SCHEMA["function"]["description"]
    analyze_image.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="analyze_image",
        category="vision",
        risk_level="low",
        capabilities=["read"],
        requires_approval=False,
    )
    analyze_image.__coworker_schema__ = _SCHEMA
    return [analyze_image]
