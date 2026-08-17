"""Build OpenAI content-parts from a user message + attachments (images, PDFs, text files).

We pass messages straight to the OpenAI SDK, which accepts `content` as either a string or an
array of parts: `{"type": "text", ...}`, `{"type": "image_url", "image_url": {"url": ...}}`
(data: URLs work, and vision models read them), and `{"type": "file", "file": {"filename",
"file_data"}}` for PDFs. So image/PDF attachments are just parts appended to the user turn —
the Anthropic/Gemini providers convert them to their own block shapes.

`build_user_content` returns a plain string when there are no attachments (back-compat with the
text-only path), else the parts list.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Optional

MAX_ATTACHMENTS = 8
MAX_IMAGE_CHARS = 12_000_000  # data-URL length cap (~8–9 MB decoded); keeps a turn sane
MAX_PDF_CHARS = 15_000_000  # data-URL length cap (~10 MB decoded, the GUI's pick limit)
MAX_TEXT_CHARS = 200_000  # per text file, inlined


def _save_image(data_url: str, name: str, dest: Path) -> Optional[str]:
    """Decode a data:image/...;base64,... URL into `dest` and return its path.
    Returns None on failure (the caller then falls back to the native part).
    Sanitizes the name and dedupes with a short hash so collisions can't overwrite."""
    try:
        header, _, b64 = data_url.partition(",")
        mime = re.match(r"data:image/(\w+)", header)
        ext = (mime.group(1) if mime else "png").lower()
        if ext not in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
            ext = "png"
        raw = base64.b64decode(b64)
        stem = re.sub(r"[^\w.-]+", "_", Path(name).stem or "image")[:48] or "image"
        import hashlib

        h = hashlib.sha1(raw).hexdigest()[:8]
        target = dest / f"{stem}_{h}.{ext}"
        dest.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return str(target)
    except Exception:
        return None


def _is_data_image(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("data:image/") and ";base64," in url


def _is_data_pdf(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("data:application/pdf;base64,")


def build_user_content(
    text: Optional[str],
    attachments: Optional[list[dict]] = None,
    *,
    save_images_to: Optional[Path] = None,
) -> Any:
    """Return `str` (no attachments) or a list of OpenAI content-parts (with attachments).

    Each attachment is `{"kind": "image"|"pdf"|"text", "name"?, "data_url"? (image/pdf),
    "text"? (text)}`.
    Invalid/oversized attachments are skipped rather than failing the turn.

    `save_images_to`: when set, image attachments are ALSO decoded and written to that
    directory (`.qunwork_attachments/`) and a `[image: <path>]` text part is added so a
    text-only model (no vision) can still "see" the picture — the path is in context and
    the `image-understanding` skill tells the worker to run its OCR/VL analyzer on it.

    The image_url part is ALWAYS kept alongside the path text (fix 2026-08):
    - vision-capable providers get the real image via image_url (the engine also re-checks
      capabilities per call);
    - the GUI's history renderer reads image_url parts, so uploaded pictures keep showing
      after a reload instead of vanishing into a bare "[image: path]" string;
    - text-only providers still get the path via the text part (the engine replaces the
      image_url part with a placeholder for them, which is harmless).
    """
    text = (text or "").strip()
    attachments = attachments or []
    if not attachments:
        return text

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})

    added = 0  # attachment parts that actually made it in
    for a in attachments[:MAX_ATTACHMENTS]:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind")
        if kind == "image":
            url = a.get("data_url") or ""
            if _is_data_image(url) and len(url) <= MAX_IMAGE_CHARS:
                if save_images_to is not None:
                    saved = _save_image(url, a.get("name") or "", save_images_to)
                    if saved:
                        parts.append(
                            {"type": "text", "text": f"[image: {saved}]"}
                        )
                        added += 1
                # Keep the real image part too (history display + vision models).
                parts.append({"type": "image_url", "image_url": {"url": url}})
                added += 1
        elif kind == "pdf":
            url = a.get("data_url") or ""
            if _is_data_pdf(url) and len(url) <= MAX_PDF_CHARS:
                name = str(a.get("name") or "attachment.pdf")
                parts.append(
                    {"type": "file", "file": {"filename": name, "file_data": url}}
                )
                added += 1
        elif kind == "text":
            body = str(a.get("text") or "")[:MAX_TEXT_CHARS]
            name = str(a.get("name") or "attachment")
            if body:
                parts.append(
                    {"type": "text", "text": f"[Attached file: {name}]\n{body}"}
                )
                added += 1

    if added == 0:
        return text  # every attachment was invalid/empty → just the text (possibly "")
    return parts


def content_to_text(content: Any, *, image_placeholder: str = "[image]") -> str:
    """Flatten message content (string or parts) to text — for titles, previews, search.
    Images render as `image_placeholder` (pass "" to drop them, e.g. for clean titles).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(str(part.get("text", "")))
            elif part.get("type") == "image_url" and image_placeholder:
                out.append(image_placeholder)
            elif part.get("type") == "file" and image_placeholder:
                out.append("[pdf]")
        return " ".join(out).strip()
    return ""
