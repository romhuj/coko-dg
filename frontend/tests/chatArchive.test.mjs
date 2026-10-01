import assert from "node:assert/strict";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const compiled = await build({ stdin: { contents: 'export * from "./src/chatArchive"; export { useChat, useApp } from "./src/store";', resolveDir: fileURLToPath(new URL("../", import.meta.url)), loader: "ts" }, bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent" });
const reply = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const message = (id, text = id, conversation_id = "one") => ({ id, text, conversation_id, role: "ai", created_at: Number(id.replace(/\D/g, "")) || 1, executed: [], dropped: [] });
function fixture(fetch = async () => reply({ conversation_id: "one", messages: [], has_more: false })) {
  const module = { exports: {} }, window = new EventTarget(); let stops = 0;
  window.addEventListener("coyote:voice-stop", () => stops++);
  vm.runInNewContext(compiled.outputFiles[0].text, { module, exports: module.exports, console, process, fetch, window, Event, AbortController, setTimeout, clearTimeout });
  const app = module.exports;
  app.useApp.getState().setState({ platform: "android", conversation_id: "one" });
  return { ...app, stops: () => stops };
}

test("HTTP and WS stable IDs deduplicate, foreign conversations cannot enter the active chat", () => {
  const f = fixture();
  f.acceptArchivedMessages("one", [message("1", "A"), message("2", "B")]);
  f.acceptArchivedMessages("one", [message("2", "B")]);
  f.acceptArchivedMessages("two", [message("3", "old conversation", "two")]);
  assert.deepEqual(Array.from(f.useChat.getState().messages, (m) => m.text), ["A", "B"]);
});

test("late initial history merges live replies; older pages preserve order and receipt fields", async () => {
  let release;
  const f = fixture(() => new Promise((resolve) => { release = resolve; }));
  const loading = f.refreshConversation();
  f.acceptArchivedMessages("one", [message("3")]);
  release(reply({ conversation_id: "one", messages: [message("1"), message("2")], has_more: true }));
  await loading;
  assert.deepEqual(Array.from(f.useChat.getState().messages, (m) => m.id), ["1", "2", "3"]);
  assert.equal(f.useChat.getState().historyHasMore, true);
  assert.equal(f.useChat.getState().historyLoading, false);
});

test("restoring another conversation cancels playback and ignores the previous fetch", async () => {
  let release;
  const f = fixture(() => new Promise((resolve) => { release = resolve; }));
  const loading = f.refreshConversation();
  f.useApp.getState().setState({ platform: "android", conversation_id: "two" });
  f.restoreConversation({ conversation_id: "two", messages: [message("4", "saved", "two")], has_more: false });
  release(reply({ conversation_id: "one", messages: [message("1")], has_more: false }));
  await loading;
  assert.equal(f.stops(), 1);
  assert.deepEqual(Array.from(f.useChat.getState().messages, (m) => m.text), ["saved"]);
});

test("busy, pending receipt, backend turn, or loading history blocks switching without clearing busy", () => {
  const f = fixture();
  for (const patch of [{ busy: true }, { awaitingConfirmation: true }, { historyLoading: true }]) {
    f.useChat.setState({ busy: false, awaitingConfirmation: false, historyLoading: false, ...patch });
    assert.equal(f.conversationSwitchBlocked(), true);
    for (const [key, value] of Object.entries(patch)) assert.equal(f.useChat.getState()[key], value);
  }
  f.useChat.setState({ busy: false, awaitingConfirmation: false, historyLoading: false });
  f.useApp.getState().setState({ platform: "android", turn_busy: true });
  assert.equal(f.conversationSwitchBlocked(), true);
  f.useApp.getState().setState({ platform: "android", turn_busy: false, pending_chat: 0 });
  assert.equal(f.conversationSwitchBlocked(), false);
});

test("device preferences restore and stale broadcasts cannot overwrite a pending manual selection", async () => {
  let release;
  const sent = [];
  const f = fixture(async (_url, init) => { sent.push(JSON.parse(init.body)); return new Promise((resolve) => { release = resolve; }); });
  const preferences = { focus_channel: "B", manual_link: true, last_presets: { A: "挤压", B: null } };
  f.useApp.getState().setState({ platform: "android", device_preferences: preferences });
  assert.equal(f.useApp.getState().focusCh, "B"); assert.equal(f.useApp.getState().linkOn, true);
  f.useApp.getState().setFocus("A");
  await new Promise((resolve) => setImmediate(resolve));
  f.useApp.getState().setState({ platform: "android", device_preferences: preferences });
  assert.equal(f.useApp.getState().focusCh, "A");
  assert.deepEqual(sent, [{ focus_channel: "A" }]);
  release(reply({ ok: true, device_preferences: { ...preferences, focus_channel: "A" } }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(f.useApp.getState().focusCh, "A"); assert.equal(f.useApp.getState().lastPreset.A, "挤压");
});
