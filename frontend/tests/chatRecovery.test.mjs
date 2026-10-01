import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const source = await readFile(new URL("../src/chatRecovery.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const recovery = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const values = new Map();
globalThis.sessionStorage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
const request = { requestId: "old-session:original-request", message: "保持现在的节奏", draft: "  保持现在的节奏\n", mode: "auto", preferredPattern: "挤压" };

test("reload recovery preserves original request ID and exact submitted payload/draft", () => {
  recovery.rememberPendingChat(request);
  assert.deepEqual(recovery.readPendingChat(), request);
  // Restoration never generates a new request ID, even after the service session changes.
  assert.equal(recovery.readPendingChat().requestId, "old-session:original-request");
});

test("a recovered receipt retains its original conversation even when another chat becomes active", () => {
  const saved = { ...request, conversationId: "original-conversation", keyboardDraft: "保留草稿" };
  recovery.rememberPendingChat(saved);
  assert.deepEqual(recovery.readPendingChat(), saved);
});
test("a definitive response only clears its own pending request", () => {
  recovery.rememberPendingChat(request);
  recovery.forgetPendingChat("different-request");
  assert.deepEqual(recovery.readPendingChat(), request);
  recovery.forgetPendingChat(request.requestId);
  assert.equal(recovery.readPendingChat(), null);
});
test("unsafe receipt blocks same-message new-ID retry including whitespace-only edits", () => {
  assert.equal(recovery.blocksUnsafeRetry("保持现在的节奏", request.message), true);
  assert.equal(recovery.blocksUnsafeRetry("  保持现在的节奏\n", request.message), true);
  assert.equal(recovery.blocksUnsafeRetry("先告诉我现在的设备状态", request.message), false);
  assert.equal(recovery.blocksUnsafeRetry(request.message, null), false);
});
test("corrupt or payload-mismatched saved requests are not submitted", () => {
  sessionStorage.setItem("coyote.pending-chat.v1", "bad JSON");
  assert.equal(recovery.readPendingChat(), null);
  sessionStorage.setItem("coyote.pending-chat.v1", JSON.stringify({ ...request, message: "不同的消息" }));
  assert.equal(recovery.readPendingChat(), null);
});

test("voice receipt recovery keeps its exact payload separate from the keyboard draft", () => {
  const voice = { ...request, origin: "voice", keyboardDraft: "正在键盘编辑的新内容" };
  recovery.rememberPendingChat(voice);
  assert.deepEqual(recovery.readPendingChat(), voice);
  assert.equal(recovery.readPendingChat().draft.trim(), voice.message);
  assert.notEqual(recovery.readPendingChat().keyboardDraft, voice.message);
});

test("editing a newer keyboard draft does not change the pending request payload", () => {
  const original = { ...request, keyboardDraft: "新草稿" };
  recovery.rememberPendingChat(original);
  const edited = { ...recovery.readPendingChat(), keyboardDraft: "新草稿继续编辑" };
  recovery.rememberPendingChat(edited);
  assert.deepEqual(recovery.readPendingChat(), edited);
  assert.equal(recovery.readPendingChat().requestId, request.requestId);
  assert.equal(recovery.readPendingChat().message, request.message);
});
