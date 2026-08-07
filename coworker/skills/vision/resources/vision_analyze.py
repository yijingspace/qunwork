#!/usr/bin/env python3
"""vision_analyze.py — local image understanding for text-only LLMs.

Reads one or more images and prints a JSON report:
  {
    "images": [
      {
        "path": "...",
        "metadata": {"width":..,"height":..,"format":"PNG","mode":"RGB","exif_time":"..."},
        "ocr": {"text":"...","lines":[{"text":"...","box":[x1,y1,x2,y2]}, ...]},
        "vision": "..." | null,
        "warnings": [...]
      }
    ]
  }

Layers (fail-soft, each degrades independently):
  1. metadata — PIL (bundled with the app)
  2. ocr — RapidOCR (rapidocr-onnxruntime, pure CPU, zh+en). If not installed,
     only metadata is produced with a warning.
  3. vision — optional OpenAI-compatible VL API via env:
       VISION_VL_BASE_URL, VISION_VL_API_KEY, VISION_VL_MODEL
     When unset, "vision" is null (fully local).

Usage:
  python vision_analyze.py <image> [<image> ...]
  python vision_analyze.py <directory>     # all images under it
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def _metadata(path: Path) -> dict:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        exif_time = None
        try:
            ex = im.getexif()
            if ex and 36867 in ex:  # DateTimeOriginal
                exif_time = str(ex[36867])
        except Exception:
            pass
        return {
            "width": im.width,
            "height": im.height,
            "format": (im.format or ""),
            "mode": im.mode,
            "exif_time": exif_time,
        }


_OCR = None


def _ocr(path: Path) -> tuple[dict | None, str | None]:
    """Return (result, warning). result: {"text":.., "lines":[...]} or None."""
    global _OCR
    try:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR = RapidOCR()
        result, _ = _OCR(str(path))
        if not result:
            return {"text": "", "lines": []}, None
        lines = []
        for item in result:
            # item: [box, text, score]
            box = item[0]
            txt = item[1]
            lines.append(
                {
                    "text": txt,
                    "box": [int(box[0][0]), int(box[0][1]), int(box[2][0]), int(box[2][1])],
                }
            )
        return {"text": "\n".join(l["text"] for l in lines), "lines": lines}, None
    except ImportError:
        return None, "rapidocr-onnxruntime not installed — OCR skipped (pip install rapidocr-onnxruntime)"
    except Exception as exc:  # noqa: BLE001
        return None, f"OCR failed: {exc}"


def _vision(path: Path) -> tuple[str | None, str | None]:
    base = os.environ.get("VISION_VL_BASE_URL")
    key = os.environ.get("VISION_VL_API_KEY")
    model = os.environ.get("VISION_VL_MODEL")
    if not (base and key and model):
        return None, None  # not configured — silent
    try:
        import urllib.request

        b64 = base64.b64encode(path.read_bytes()).decode()
        ext = path.suffix.lstrip(".").lower() or "png"
        if ext == "jpg":
            ext = "jpeg"
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image in detail (Chinese)."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{ext};base64,{b64}"},
                        },
                    ],
                }
            ],
            "max_tokens": 600,
        }
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"], None
    except Exception as exc:  # noqa: BLE001
        return None, f"VL API failed: {exc}"


def _images_from_args(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    out.append(f)
        elif p.is_file():
            out.append(p)
    return out


def main() -> int:
    # Worker shells decode our stdout as UTF-8 — never let Windows locale
    # (GBK) mangle the JSON we print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: vision_analyze.py <image|dir> [...]"}, ensure_ascii=False))
        return 2
    images = _images_from_args(sys.argv[1:])
    if not images:
        print(json.dumps({"error": "no readable images found"}, ensure_ascii=False))
        return 1

    report = {"images": []}
    for img in images:
        entry: dict = {"path": str(img)}
        try:
            entry["metadata"] = _metadata(img)
        except Exception as exc:  # noqa: BLE001
            entry["metadata"] = {}
            entry.setdefault("warnings", []).append(f"metadata failed: {exc}")
        ocr, ocr_warn = _ocr(img)
        entry["ocr"] = ocr
        if ocr_warn:
            entry.setdefault("warnings", []).append(ocr_warn)
        vision, vis_warn = _vision(img)
        entry["vision"] = vision
        if vis_warn:
            entry.setdefault("warnings", []).append(vis_warn)
        report["images"].append(entry)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
