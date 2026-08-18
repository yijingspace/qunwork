import type { Attachment } from "./types";

const MAX_BYTES = 10 * 1024 * 1024; // PDF/text 附件上限
// 图片超过该体积时自动压缩后再发送(2026-08-18 owner bug: 14.7MB 图上传后
// "LLM 没反应" — 超 MAX_IMAGE_CHARS 被服务端拒 + 超 WS 帧 16MB 发不出)。
const IMAGE_COMPRESS_THRESHOLD = 4 * 1024 * 1024;
const TEXT_RE =
  /\.(txt|md|markdown|csv|tsv|json|ya?ml|log|ini|toml|py|js|ts|tsx|jsx|rs|go|java|c|h|cpp|sh|html?|css|sql|xml)$/i;
// Image detection MUST fall back to the file extension: the desktop shell's file
// picker (Windows WebView2) can return an empty `file.type` for local files, and
// then `file.type.startsWith("image/")` is false — the picture would be silently
// dropped with no chip and no notice (owner bug 2026-08-18: picking a photo in the
// + menu, coming back, no image in the composer). PDFs already used isPdfFile's
// extension fallback; images need the same.
const IMAGE_RE =
  /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic|heif|tiff?|ico)$/i;

// Read a File into an Attachment (image/PDF → data URL, text → inline text). Returns null for
// unsupported types or oversized files. Shared by the composer and the session start panel.
export const isPdfFile = (file: File) =>
  file.type === "application/pdf" || /\.pdf$/i.test(file.name);

export const isImageFile = (file: File) =>
  file.type.startsWith("image/") || IMAGE_RE.test(file.name);

export const isOversized = (file: File) =>
  !isImageFile(file) && file.size > MAX_BYTES;

// Why a file was not attached — surfaced to the user instead of silent dropping
// (before the fix, an oversized or untypable image just vanished).
export function rejectReason(file: File): string | null {
  if (isOversized(file)) {
    return `${file.name} skipped — ${(file.size / 1024 / 1024).toFixed(1)} MB is over the 10 MB attachment limit`;
  }
  const isPdf = isPdfFile(file);
  const isImage = isImageFile(file);
  const isText = !isPdf && (file.type.startsWith("text/") || TEXT_RE.test(file.name));
  if (!isPdf && !isImage && !isText) {
    return `${file.name} skipped — unsupported file type`;
  }
  return null;
}

// Downscale a large image via canvas so it fits the server's 12MB data-URL cap and the
// 16MB WS frame (owner bug 2026-08-18: 14.7MB PNG 4096×3072 → "LLM 没反应").
// IMPORTANT: the image is loaded from `URL.createObjectURL(file)` — a ~19MB base64
// `data:` URL fed straight to <img> can fail to load in WebView2/Chromium (data-URL
// size limits), which silently skipped compression and sent the original oversized
// frame. Blob URLs have no such limit. Transparent images stay PNG; opaque → JPEG.
// Returns "" when the image is already small enough or rendering failed (caller keeps
// the original — for a genuinely unrenderable format there is nothing else to do).
function renderImage(file: File, edge: number, quality: number, mime: string): Promise<string> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      try {
        const scale = Math.min(1, edge / Math.max(img.width, img.height));
        if (scale >= 1) {
          resolve(""); // already fits — no compression needed
          return;
        }
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          resolve("");
          return;
        }
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const hasAlpha = mime === "image/png" || mime === "image/webp" || mime.includes("png");
        const out = canvas.toDataURL(hasAlpha ? "image/png" : "image/jpeg", hasAlpha ? undefined : quality);
        resolve(out || "");
      } catch {
        resolve("");
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve("");
    };
    img.src = url;
  });
}

// Progressive downscale: try smaller edge/quality until the result fits the 8MB target
// (keeps the whole message safely under the 16MB WS frame and 12MB image cap).
async function compressToFit(file: File, mime: string): Promise<string> {
  const attempts: Array<[number, number]> = [
    [2048, 0.85],
    [1600, 0.75],
    [1280, 0.65],
    [1024, 0.55],
  ];
  for (const [edge, quality] of attempts) {
    const out = await renderImage(file, edge, quality, mime);
    if (out && out.length < 8_000_000) return out;
  }
  return "";
}

export async function compressAttachment(a: Attachment): Promise<Attachment> {
  // Desktop shell's native picker (pick_images) returns the RAW base64 data_url with no
  // size check (owner bug 2026-08-18: 14.3MB PNG → ~19MB data_url → exceeds the 12MB
  // image cap and the 16MB WS frame → connection dropped → "LLM 没反应"). Re-compress
  // here so the Tauri path behaves like the HTML/drag path.
  if (!a.data_url || a.data_url.length <= 8_000_000) return a;
  try {
    // atob decode (not fetch(data_url)) — fetching a ~19MB data: URL can itself fail in
    // WebView2, which would skip compression and send the original oversized frame.
    const comma = a.data_url.indexOf(",");
    const mime = a.data_url.slice(5, comma).split(";")[0] || a.mime || "image/png";
    const bin = atob(a.data_url.slice(comma + 1));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const file = new File([bytes], a.name || "image", { type: mime });
    const out = await compressToFit(file, mime);
    if (out) {
      return {
        ...a,
        data_url: out,
        mime: out.startsWith("data:image/png") ? "image/png" : "image/jpeg",
      };
    }
  } catch {
    /* keep the original — server-side validation is the backstop */
  }
  return a;
}

export function readFile(file: File): Promise<Attachment | null> {
  const isImage = isImageFile(file);
  const isPdf = isPdfFile(file);
  const isText = !isPdf && (file.type.startsWith("text/") || TEXT_RE.test(file.name));
  if ((!isImage && !isPdf && !isText) || isOversized(file)) return Promise.resolve(null);
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onerror = () => resolve(null);
    reader.onload = () => {
      if (isImage) {
        // Build the data URL OURSELVES with the real image MIME: a File with an
        // empty `type` (Windows WebView2 local picker) makes readAsDataURL emit
        // `data:application/octet-stream`, which the server rejects (it requires
        // a `data:image/` prefix) — the picture would be dropped server-side.
        const mime = file.type || mimeFromName(file.name) || "image/png";
        const base64 = typeof reader.result === "string"
          ? reader.result.split(",")[1] || ""
          : "";
        const dataUrl = `data:${mime};base64,${base64}`;
        // Large pictures are compressed before sending — otherwise they exceed the
        // server's MAX_IMAGE_CHARS and the WS frame cap and the turn never starts.
        if (file.size > IMAGE_COMPRESS_THRESHOLD || dataUrl.length > 8_000_000) {
          compressToFit(file, mime).then((compressed) => {
            if (compressed) {
              resolve({
                kind: "image",
                name: file.name || "image",
                mime: compressed.startsWith("data:image/png") ? "image/png" : "image/jpeg",
                data_url: compressed,
              });
            } else {
              // 压缩失败(格式无法渲染等) — 原样发送, 附缩略图仍显示;
              // 超限由服务端校验兜底(不会静默无响应 — 会有错误提示)。
              resolve({ kind: "image", name: file.name || "image", mime, data_url: dataUrl });
            }
          });
        } else {
          resolve({ kind: "image", name: file.name || "image", mime, data_url: dataUrl });
        }
        return;
      }
      resolve(
        isPdf
          ? { kind: "pdf", name: file.name || "file.pdf", mime: "application/pdf", data_url: String(reader.result) }
          : { kind: "text", name: file.name || "file.txt", mime: file.type, text: String(reader.result) },
      );
    };
    if (isImage || isPdf) reader.readAsDataURL(file);
    else reader.readAsText(file);
  });
}

function mimeFromName(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() || "";
  const table: Record<string, string> = {
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    gif: "image/gif",
    webp: "image/webp",
    bmp: "image/bmp",
    svg: "image/svg+xml",
    avif: "image/avif",
    heic: "image/heic",
    heif: "image/heif",
    tif: "image/tiff",
    tiff: "image/tiff",
    ico: "image/x-icon",
  };
  return table[ext] || "";
}
