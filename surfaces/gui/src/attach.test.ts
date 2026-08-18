// attach.ts — file → Attachment mapping, with the desktop-shell empty-file.type
// regression covered (Windows WebView2 returns "" for local files; image detection
// must fall back to the extension or the picked photo silently vanishes).
import { describe, expect, it } from "vitest";
import { isImageFile, readFile, rejectReason } from "./attach";

const PNG_BYTES = new Uint8Array([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49,
  0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06,
  0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44,
  0x41, 0x54, 0x78, 0x9c, 0x63, 0xf8, 0xcf, 0xc0, 0x50, 0x0f, 0x00, 0x04, 0x85,
  0x01, 0x80, 0xa1, 0x29, 0x8c, 0x21, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
  0x44, 0xae, 0x42, 0x60, 0x82,
]);

const makeFile = (name: string, type = ""): File =>
  new File([PNG_BYTES], name, { type });

describe("isImageFile — extension fallback for empty MIME", () => {
  it("recognizes images by extension when file.type is empty (Windows WebView2)", () => {
    expect(isImageFile(makeFile("photo.png", ""))).toBe(true);
    expect(isImageFile(makeFile("photo.JPG", ""))).toBe(true);
    expect(isImageFile(makeFile("photo.jpeg", ""))).toBe(true);
    expect(isImageFile(makeFile("photo.gif", ""))).toBe(true);
    expect(isImageFile(makeFile("photo.webp", ""))).toBe(true);
    expect(isImageFile(makeFile("photo.heic", ""))).toBe(true);
  });

  it("recognizes images by MIME when present", () => {
    expect(isImageFile(makeFile("x", "image/png"))).toBe(true);
  });

  it("rejects non-images", () => {
    expect(isImageFile(makeFile("doc.txt", ""))).toBe(false);
    expect(isImageFile(makeFile("data.json", ""))).toBe(false);
  });
});

describe("readFile", () => {
  it("reads an image with empty file.type via extension fallback", async () => {
    const a = await readFile(makeFile("截图.png", ""));
    expect(a).not.toBeNull();
    expect(a!.kind).toBe("image");
    expect(a!.name).toBe("截图.png");
    expect(a!.data_url).toContain("data:image/");
    expect(a!.data_url).toContain(";base64,");
  });

  it("returns null for oversized non-image files", async () => {
    // 图片现在自动压缩不再拒绝(2026-08-18); PDF/text 仍超限即拒。
    const bigPdf = new File([new Uint8Array(11 * 1024 * 1024)], "big.pdf", { type: "application/pdf" });
    expect(await readFile(bigPdf)).toBeNull();
  });
});

describe("rejectReason — no more silent drops", () => {
  it("explains oversized attachments", () => {
    const big = new File([new Uint8Array(11 * 1024 * 1024)], "big.pdf", { type: "application/pdf" });
    expect(rejectReason(big)).toContain("10 MB");
  });

  it("explains unsupported types", () => {
    expect(rejectReason(makeFile("archive.zip", ""))).toContain("unsupported");
  });

  it("returns null for acceptable files", () => {
    expect(rejectReason(makeFile("photo.png", ""))).toBeNull();
    expect(rejectReason(makeFile("note.txt", ""))).toBeNull();
    expect(rejectReason(makeFile("doc.pdf", "application/pdf"))).toBeNull();
  });
});
