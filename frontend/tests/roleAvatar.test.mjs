import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
const code = await build({ stdin: { contents: 'export * from "./src/roleAvatar"; export * from "./src/channelNames";', resolveDir: fileURLToPath(new URL("../", import.meta.url)), loader: "ts" }, bundle: true, platform: "node", format: "cjs", write: false });
const module = { exports: {} }; vm.runInNewContext(code.outputFiles[0].text, { exports: module.exports, module });
const { validateAvatarFile, avatarCrop, checkAvatarDimensions, localRoleAvatar, channelLabel } = module.exports;

test("avatar rejects oversized originals, empty files, SVG and unsupported image types", () => {
  validateAvatarFile({ type: "image/jpeg", size: 1000 });
  for (const file of [{ type: "image/png", size: 13 * 1024 * 1024 }, { type: "image/jpeg", size: 0 }, { type: "image/svg+xml", size: 1024 }, { type: "text/html", size: 500 }]) assert.throws(() => validateAvatarFile(file));
});
test("avatar crop centers landscape and portrait pictures and bounds decoded pixels", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(avatarCrop(1200, 800))), { x: 200, y: 0, size: 800 });
  assert.deepEqual(JSON.parse(JSON.stringify(avatarCrop(600, 900))), { x: 0, y: 150, size: 600 });
  assert.throws(() => avatarCrop(8000, 6000)); assert.throws(() => avatarCrop(0, 100));
});
test("pixel limits are checked from PNG and JPEG headers before image decoding", () => {
  const png = new ArrayBuffer(24), bytes = new Uint8Array(png), view = new DataView(png); bytes.set([0x89, 80, 78, 71]);
  view.setUint32(16, 512); view.setUint32(20, 512); checkAvatarDimensions(png, "image/png");
  view.setUint32(16, 10000); view.setUint32(20, 10000); assert.throws(() => checkAvatarDimensions(png, "image/png"));
  const jpeg = new Uint8Array([255,216,255,192,0,8,8,2,0,2,0,0]).buffer; checkAvatarDimensions(jpeg, "image/jpeg");
  assert.throws(() => checkAvatarDimensions(new ArrayBuffer(4), "image/jpeg"));
});
test("saved avatar URLs stay on the authenticated local endpoint", () => {
  assert.equal(localRoleAvatar("/api/character/avatar?role=assistant&v=abcd"), "/api/character/avatar?role=assistant&v=abcd");
  for (const input of ["https://example.com/avatar.jpg", "//example.com/avatar", "data:image/jpeg;base64,YQ==", "/api/character/assistant/avatar", "/api/character/avatar?role=x#remote"]) assert.equal(localRoleAvatar(input), undefined);
});
test("custom channel names never replace A/B channel identity", () => {
  assert.equal(channelLabel("A", { A: { name: "左侧" } }), "A · 左侧");
  assert.equal(channelLabel("B", { B: { name: "B 通道" } }), "B 通道");
  assert.equal(channelLabel("A"), "A 通道");
});
