export const AVATAR_ORIGINAL_LIMIT = 12 * 1024 * 1024;
export const AVATAR_EDGE = 512;
const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/x-ms-bmp"]);

export function validateAvatarFile(file: Pick<File, "size" | "type">): void {
  if (file.size <= 0) throw new Error("这张图片为空，请重新选择。");
  if (file.size > AVATAR_ORIGINAL_LIMIT) throw new Error("图片不能超过 12 MB，请选择较小的图片。");
  if (!IMAGE_TYPES.has(file.type.toLowerCase())) throw new Error("请选择 JPG、PNG、WebP 等常见图片，暂不支持此格式。");
}

export function avatarCrop(width: number, height: number): { x: number; y: number; size: number } {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width < 1 || height < 1 || width * height > 32_000_000) throw new Error("图片超过 3200 万像素或无法读取，请换一张图片。");
  const size = Math.min(width, height);
  return { x: (width - size) / 2, y: (height - size) / 2, size };
}

/** Reject excessive pixel counts from the container header before decoding pixels. */
export function checkAvatarDimensions(buffer: ArrayBuffer, type: string): void {
  const bytes = new Uint8Array(buffer), view = new DataView(buffer);
  const text = (at: number, size: number) => String.fromCharCode(...bytes.slice(at, at + size));
  let width = 0, height = 0;
  if (type === "image/png" && bytes.length >= 24 && text(1, 3) === "PNG") { width = view.getUint32(16); height = view.getUint32(20); }
  else if (type === "image/gif" && bytes.length >= 10 && text(0, 3) === "GIF") { width = view.getUint16(6, true); height = view.getUint16(8, true); }
  else if (type === "image/bmp" || type === "image/x-ms-bmp") { if (bytes.length >= 26 && text(0, 2) === "BM") { width = Math.abs(view.getInt32(18, true)); height = Math.abs(view.getInt32(22, true)); } }
  else if (type === "image/webp" && bytes.length >= 30 && text(0, 4) === "RIFF" && text(8, 4) === "WEBP") {
    const kind = text(12, 4);
    if (kind === "VP8X") { width = 1 + bytes[24] + bytes[25] * 256 + bytes[26] * 65536; height = 1 + bytes[27] + bytes[28] * 256 + bytes[29] * 65536; }
    else if (kind === "VP8 ") { width = view.getUint16(26, true) & 0x3fff; height = view.getUint16(28, true) & 0x3fff; }
    else if (kind === "VP8L" && bytes[20] === 0x2f) { const packed = view.getUint32(21, true); width = 1 + (packed & 0x3fff); height = 1 + ((packed >>> 14) & 0x3fff); }
  } else if (type === "image/jpeg" && bytes[0] === 0xff && bytes[1] === 0xd8) {
    let at = 2;
    while (at + 8 < bytes.length) {
      if (bytes[at++] !== 0xff) break;
      while (bytes[at] === 0xff) at++;
      const marker = bytes[at++];
      if (marker === 0xd9 || marker === 0xda || at + 2 > bytes.length) break;
      const length = view.getUint16(at);
      if (length < 2 || at + length > bytes.length) break;
      if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker) && length >= 7) { height = view.getUint16(at + 3); width = view.getUint16(at + 5); break; }
      at += length;
    }
  }
  avatarCrop(width, height);
}

/** Rasterize locally to strip source metadata and bound the local-service upload. */
export async function prepareRoleAvatar(file: File): Promise<string> {
  validateAvatarFile(file);
  checkAvatarDimensions(await file.arrayBuffer(), file.type.toLowerCase());
  const url = URL.createObjectURL(file);
  try {
    const picture = new Image();
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => { picture.src = ""; reject(new Error("图片读取超时，请换一张图片。")); }, 15000);
      picture.onload = () => { window.clearTimeout(timer); resolve(); };
      picture.onerror = () => { window.clearTimeout(timer); reject(new Error("无法读取这张图片，请选择 JPG、PNG 或 WebP 图片。")); };
      picture.src = url;
    });
    const crop = avatarCrop(picture.naturalWidth, picture.naturalHeight);
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = Math.min(AVATAR_EDGE, crop.size);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("当前无法处理图片，请稍后重试。");
    context.fillStyle = "#202020"; context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(picture, crop.x, crop.y, crop.size, crop.size, 0, 0, canvas.width, canvas.height);
    const encoded = canvas.toDataURL("image/jpeg", 0.86);
    if (!encoded.startsWith("data:image/jpeg;base64,") || encoded.length > 680_000) throw new Error("图片处理失败，请选择较小的图片。");
    return encoded;
  } finally { URL.revokeObjectURL(url); }
}

/** Saved avatars come from the authenticated local service only. */
export function localRoleAvatar(value?: string | null): string | undefined {
  return typeof value === "string" && /^\/api\/character\/avatar\?role=[^&#]+(?:&v=[A-Za-z0-9_-]+)?$/.test(value) ? value : undefined;
}
