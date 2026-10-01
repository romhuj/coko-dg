import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import ts from "typescript";
import vm from "node:vm";
import test from "node:test";

const source = await readFile(new URL("../src/pairingBridge.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } });
function fixture() {
  const module = { exports: {} }, posts = [];
  const bridge = { onmessage: null, postMessage: (value) => posts.push(JSON.parse(value)) };
  vm.runInNewContext(outputText, { module, exports: module.exports, window: { CoyotePairing: bridge }, crypto: webcrypto, setTimeout, clearTimeout });
  return { ...module.exports, posts, bridge };
}
test("native copy/open has a unique request ID, ignores unrelated callbacks, and returns missing-app copy success", async () => {
  const f = fixture(); let previous = 0;
  f.bridge.onmessage = () => previous++;
  const original = f.bridge.onmessage;
  const promise = f.copyPairingLink("https://dungeon-lab.cn/s/?test", true);
  assert.equal(f.posts[0].type, "copyAndOpen");
  assert.match(f.posts[0].requestId, /^[A-Za-z0-9_-]{8,96}$/);
  f.bridge.onmessage({ data: JSON.stringify({ type: "result", requestId: "other-request", status: "opened", copied: true }) });
  assert.equal(previous, 1);
  f.bridge.onmessage({ data: JSON.stringify({ type: "result", requestId: f.posts[0].requestId, status: "notInstalled", copied: true, opened: false, message: "链接已复制，未安装 DG-LAB4" }) });
  const result = await promise;
  assert.equal(result.status, "notInstalled"); assert.equal(result.copied, true); assert.equal(result.opened, false);
  assert.equal(f.bridge.onmessage, original);
  const next = f.copyPairingLink("https://dungeon-lab.cn/s/?test", false);
  assert.notEqual(f.posts[0].requestId, f.posts[1].requestId);
  f.bridge.onmessage({ data: JSON.stringify({ type: "result", requestId: f.posts[1].requestId, status: "copied", copied: true }) });
  await next;
});
