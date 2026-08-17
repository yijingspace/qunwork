import type { Attachment } from "./types";

const MAX_BYTES = 10 * 1024 * 1024; // skip files larger than ~10MB
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

export const isOversized = (file: File) => file.size > MAX_BYTES;

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
        resolve({ kind: "image", name: file.name || "image", mime, data_url: `data:${mime};base64,${base64}` });
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
