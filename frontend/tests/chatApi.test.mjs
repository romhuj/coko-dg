import assert from "node:assert/strict";
import { build } from "esbuild";
import { webcrypto } from "node:crypto";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const root = fileURLToPath(new URL("../", import.meta.url));
const compiled = await build({ stdin: { contents: 'export { api, ChatTransportError, ChatUnsafeRetryError } from "./src/api"; export { useApp } from "./src/store";', resolveDir: root, loader: "ts" }, bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent" });
const requestId = `${"a".repeat(32)}:12345678-1234-1234-1234-123456789abc`;
const result = { line: "已处理", executed: [], dropped: [] };
const reply = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const pending = () => reply({ status: "pending", request_id: requestId }, 202);
const complete = (value = result) => reply({ status: "completed", request_id: requestId, result: value });
function harness(fetch) {
  const module = { exports: {} };
  const context = { module, exports: module.exports, console, process, AbortController, crypto: webcrypto, Error, TypeError, SyntaxError, fetch,
    // Poll delays are shortened; abort deadlines remain normal and are cleared by each completed fetch.
    setTimeout: (callback, delay) => setTimeout(callback, delay === 800 ? 0 : delay), clearTimeout };
  vm.runInNewContext(compiled.outputFiles[0].text, context);
  return { ...module.exports, context };
}

test("202 receipt polls the same ID and returns actual result", async () => {
  const calls = [];
  const app = harness(async (path, init) => { calls.push([path, init]); return calls.length === 1 ? pending() : complete(); });
  const received = await app.api.chat("你好", "auto", undefined, requestId);
  assert.deepEqual(received, result);
  assert.equal(JSON.parse(calls[0][1].body).request_id, requestId);
  assert.equal(calls[1][0], `/api/chat/result/${encodeURIComponent(requestId)}`);
  assert.equal(calls.length, 2);
});

test("conversation guards are transmitted with and without an idempotent request ID; detail errors stay readable", async () => {
  const posts = [];
  const app = harness(async (_path, init) => { const body = JSON.parse(init.body); posts.push(body); return body.request_id ? complete() : reply(result); });
  await app.api.chat("你好", "auto", undefined, requestId, "saved-chat");
  await app.api.chat("你好", "auto", undefined, undefined, "saved-chat");
  assert.deepEqual(posts.map((body) => body.conversation_id), ["saved-chat", "saved-chat"]);
  const failed = harness(async () => reply({ detail: "聊天记录暂时不可用" }, 503));
  await assert.rejects(failed.api.conversationMessages("one"), /聊天记录暂时不可用/);
});
test("response loss is ambiguous and retry keeps exact payload/ID", async () => {
  const posts = [];
  const accepted = new Set();
  const app = harness(async (_path, init) => {
    const body = JSON.parse(init.body); posts.push(body); accepted.add(body.request_id);
    if (posts.length === 1) throw new TypeError("network lost after server accepted");
    return complete();
  });
  await assert.rejects(app.api.chat("保持现在的节奏", "auto", "挤压", requestId), (error) => error instanceof app.ChatTransportError && error.requestId === requestId);
  await app.api.chat("保持现在的节奏", "auto", "挤压", requestId);
  assert.deepEqual(posts[0], posts[1]);
  assert.equal(accepted.size, 1);
});
test("500 and missing GET receipt stay ambiguous instead of allowing a new action request", async () => {
  const broken = harness(async () => reply({ error: "internal error" }, 500));
  await assert.rejects(broken.api.chat("你好", "auto", undefined, requestId), (error) => error instanceof broken.ChatTransportError);
  let count = 0;
  const absent = harness(async () => ++count === 1 ? pending() : reply({ error: "not received" }, 404));
  await assert.rejects(absent.api.chat("你好", "auto", undefined, requestId), (error) => error instanceof absent.ChatTransportError);
});
test("409 and 410 stop same-message new-ID retries; ordinary rejected inputs are definitive", async () => {
  for (const status of [409, 410]) {
    const app = harness(async () => reply({ error: "cannot confirm prior outcome" }, status));
    await assert.rejects(app.api.chat("你好", "auto", undefined, requestId), (error) => error instanceof app.ChatUnsafeRetryError && error.requestId === requestId);
  }
  for (const status of [400, 429]) {
    const app = harness(async () => reply({ error: "rejected before execution" }, status));
    await assert.rejects(app.api.chat("你好", "auto", undefined, requestId), (error) => error instanceof Error && !(error instanceof app.ChatTransportError) && !(error instanceof app.ChatUnsafeRetryError));
  }
});
test("malformed completed receipts cannot be treated as safe model failures", async () => {
  for (const body of [{ status: "completed", request_id: "other-id", result }, { status: "completed", request_id: requestId, result: {} }]) {
    const app = harness(async () => reply(body));
    await assert.rejects(app.api.chat("你好", "auto", undefined, requestId), (error) => error instanceof app.ChatTransportError);
  }
});
test("completed model failure is preserved for UI retry policy and is not a transport loss", async () => {
  const app = harness(async () => complete({ ...result, line: "", error: "模型没有返回内容", retryable: true }));
  const received = await app.api.chat("你好", "auto", undefined, requestId);
  assert.equal(received.error, "模型没有返回内容");
  assert.equal(received.retryable, true);
  const partial = harness(async () => complete({ ...result, line: "", error: "部分操作可能已执行", retryable: false }));
  assert.equal((await partial.api.chat("你好", "auto", undefined, requestId)).retryable, false);
});
