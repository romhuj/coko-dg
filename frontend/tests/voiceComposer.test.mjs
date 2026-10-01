// Real React hook/component tests with a fake native bridge and mocked HTTP only.
// Uses the project's matching React test renderer; REACT_TEST_RUNTIME optionally overrides its npm prefix.
import assert from "node:assert/strict";
import { build } from "esbuild";
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const workspace = fileURLToPath(new URL("../../", import.meta.url));
const runtime = process.env.REACT_TEST_RUNTIME || path.join(workspace, "frontend");
const runtimeRequire = createRequire(path.join(runtime, "package.json"));
const React = runtimeRequire("react");
const { act, create } = runtimeRequire("react-test-renderer");
const componentTest = test;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const compiled = await build({ stdin: { contents: 'export { default as ChatComposer } from "./src/components/ChatComposer"; export { useApp, useChat } from "./src/store";', resolveDir: path.join(workspace, "frontend"), loader: "ts" }, bundle: true, platform: "node", format: "cjs", external: ["react", "react/jsx-runtime"], write: false, logLevel: "silent" });
const response = (body) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
const complete = (body) => response({ status: "completed", request_id: body.request_id, result: { line: "收到", executed: [], dropped: [] } });
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
const flush = () => new Promise((resolve) => setImmediate(resolve));

async function fixture(fetchHandler) {
  const requests = [], posts = [], speechPosts = [], previewNodes = [], storage = new Map();
  const document = new EventTarget(); document.hidden = false;
  const window = new EventTarget(); window.innerWidth = 400;
  window.CoyoteVoice = { onmessage: null, postMessage: (data) => posts.push(JSON.parse(data)) };
  window.CoyoteSpeech = { onmessage: null, postMessage: (data) => speechPosts.push(JSON.parse(data)) };
  const module = { exports: {} };
  const context = { module, exports: module.exports, require: runtimeRequire, console, process,
    window, document, Event, crypto: webcrypto, AbortController, Error, TypeError, SyntaxError, setTimeout, clearTimeout,
    sessionStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    fetch: async (url, init) => { const body = JSON.parse(init.body); requests.push({ url, body }); return fetchHandler ? fetchHandler(url, body, requests.length) : complete(body); },
  };
  vm.runInNewContext(compiled.outputFiles[0].text, context);
  const app = module.exports;
  app.useApp.setState({ state: { platform: "android", chat_session_id: "test-session", role: "测试角色", profile: "默认", lang: "zh", estop: false, autopilot: false, presets: [] } });
  let rendered;
  await act(async () => { rendered = create(React.createElement(app.ChatComposer, { mode: "auto", onModeChange: () => {}, paired: false }), {
    createNodeMock: (element) => {
      if (element.props.className === "android-voice-preview") {
        const node = { scrollLeft: 0, scrollWidth: 1400 };
        previewNodes.push(node); return node;
      }
      return null;
    },
  }); });
  const find = (type, predicate) => rendered.root.findAllByType(type).find((node) => predicate(node.props));
  const input = () => find("textarea", (props) => props.id === "chat-message");
  const button = (label) => find("button", (props) => props["aria-label"] === label || props.children === label);
  const draft = async (text) => { await act(async () => input().props.onChange({ target: { value: text, style: {}, scrollHeight: 48 } })); };
  const event = async (data) => { await act(async () => { window.CoyoteVoice.onmessage?.({ data: JSON.stringify({ sessionId: posts.filter((item) => item.type === "start").at(-1)?.sessionId, ...data }) }); await flush(); }); };
  const start = async () => { await act(async () => button("开启持续语音输入").props.onClick()); await event({ type: "state", status: "listening" }); };
  const speechEvent = async (data) => { await act(async () => { window.CoyoteSpeech.onmessage?.({ data: JSON.stringify({ type: "state", sessionId: speechPosts.filter((item) => item.type === "enable").at(-1)?.sessionId, ...data }) }); await flush(); }); };
  const enableSpeech = async () => { await act(async () => button("开启回复朗读").props.onClick()); await speechEvent({ status: "ready" }); };
  const sentence = (id, text) => event({ type: "sentence", utteranceId: id, text });
  const close = async () => { await act(async () => rendered.unmount()); };
  return { ...app, requests, posts, speechPosts, speechEvent, enableSpeech, previewNodes, storage, context, rendered, input, button, draft, event, start, sentence, close };
}

componentTest("live preview scrolls to its tail and volume responds without sending or editing the keyboard draft", async () => {
  const f = await fixture();
  try {
    await f.draft("保留键盘草稿"); await f.start();
    const preview = () => f.rendered.root.findByProps({ className: "android-voice-preview" });
    assert.equal(preview().props.children, "正在聆听…");
    const text = "持续识别的长句".repeat(40);
    f.previewNodes.at(-1).scrollLeft = 0;
    await f.event({ type: "preview", text });
    assert.equal(preview().props.children, text); assert.equal(preview().props["aria-live"], "off");
    assert.equal(f.previewNodes.at(-1).scrollLeft, f.previewNodes.at(-1).scrollWidth);
    await f.event({ type: "level", level: 0.75 });
    assert.equal(f.button("停止语音输入").props.style["--voice-level"], 0.75);
    assert.equal(f.input().props.value, "保留键盘草稿"); assert.equal(f.requests.length, 0);
    await f.event({ type: "state", status: "listening", reason: "playback" });
    assert.equal(preview().props.children, "正在朗读 · 暂停识别");
    assert.equal(f.button("停止语音输入").props.style["--voice-level"], 0);
    await act(async () => f.button("停止语音输入").props.onClick());
    assert.equal(f.rendered.root.findAllByProps({ className: "android-voice-preview" }).length, 0);
    assert.equal(f.button("开启持续语音输入").props.style["--voice-level"], 0);
  } finally { await f.close(); }
});

componentTest("playback uses persisted role voice and legacy roles do not initiate an offline voice download", async () => {
  const f = await fixture();
  try {
    await f.enableSpeech();
    assert.equal(f.speechPosts.find((item) => item.type === "enable").voiceId, "system-default");
    await act(async () => f.button("关闭回复朗读").props.onClick());
    await act(async () => f.useApp.setState({ state: { ...f.useApp.getState().state, roles: [{ name: "测试角色", voiceId: "kokoro-zm_010" }] } }));
    await f.enableSpeech();
    assert.equal(f.speechPosts.filter((item) => item.type === "enable").at(-1).voiceId, "kokoro-zm_010");
  } finally { await f.close(); }
});

componentTest("voice keeps keyboard draft and queued speech shares the typed sender's synchronous lock", async () => {
  const gate = deferred();
  const f = await fixture(async (_url, body, index) => index === 1 ? gate.promise : complete(body));
  try {
    await f.draft("键盘草稿"); await f.start(); await f.sentence("one", "第一句语音");
    await f.draft("在等待期间编辑的新草稿"); await f.sentence("two", "第二句语音");
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].body.message, "第一句语音");
    await act(async () => { gate.resolve(complete(f.requests[0].body)); await flush(); });
    assert.equal(f.requests.length, 2);
    assert.notEqual(f.requests[0].body.request_id, f.requests[1].body.request_id);
    assert.equal(f.input().props.value, "在等待期间编辑的新草稿");
    assert.equal(f.useChat.getState().messages.filter((item) => item.role === "user").length, 2);
  } finally { await f.close(); }
});

componentTest("rapid typed double submit makes one request and its late response preserves a newer draft", async () => {
  const gate = deferred(); const f = await fixture(() => gate.promise);
  try {
    await f.draft("原消息");
    await act(async () => {
      const submit = f.rendered.root.findByType("form").props.onSubmit;
      submit({ preventDefault() {} }); submit({ preventDefault() {} });
    });
    assert.equal(f.requests.length, 1);
    await f.draft("下一条新草稿");
    await act(async () => { gate.resolve(complete(f.requests[0].body)); await flush(); });
    assert.equal(f.input().props.value, "下一条新草稿");
  } finally { await f.close(); }
});

componentTest("ambiguous voice result blocks later auto-send and confirms the same ID without losing keyboard text", async () => {
  let fail = true;
  const f = await fixture(async (_url, body) => { if (fail) throw new TypeError("response lost"); return complete(body); });
  try {
    await f.draft("独立键盘草稿"); await f.start(); await f.sentence("one", "可能已执行的语音");
    assert.equal(f.useChat.getState().awaitingConfirmation, true);
    assert.equal(f.input().props.value, "独立键盘草稿");
    const saved = JSON.parse(f.storage.get("coyote.pending-chat.v1"));
    assert.equal(saved.origin, "voice"); assert.equal(saved.keyboardDraft, "独立键盘草稿");
    await f.draft("确认前继续编辑"); fail = false;
    await act(async () => { await f.button("确认上次请求").props.onClick(); await flush(); });
    assert.equal(f.requests.length, 2);
    assert.deepEqual(f.requests[0].body, f.requests[1].body);
    assert.equal(f.input().props.value, "确认前继续编辑");
    assert.equal(f.useChat.getState().awaitingConfirmation, false);
  } finally { await f.close(); }
});

componentTest("definitive model failure leaves editable transcription separate from keyboard draft", async () => {
  const f = await fixture(async (_url, body) => response({ status: "completed", request_id: body.request_id, result: { line: "", executed: [], dropped: [], error: "模型暂时不可用" } }));
  try {
    await f.draft("键盘草稿"); await f.start(); await f.sentence("one", "识别出来的句子");
    const review = f.rendered.root.findAllByType("textarea").find((node) => node.props.id === "voice-review");
    assert.equal(review.props.value, "识别出来的句子");
    assert.equal(f.input().props.value, "键盘草稿");
    assert.equal(f.requests.length, 1);
    assert.equal(f.useChat.getState().busy, false);
  } finally { await f.close(); }
});

componentTest("hidden cached chat cancels queued speech and rejects a late native sentence", async () => {
  const f = await fixture();
  try {
    await f.start(); await act(async () => f.useChat.getState().setBusy(true));
    await f.sentence("one", "不要跨页面发送");
    const old = f.context.window.CoyoteVoice.onmessage;
    const sessionId = f.posts.find((item) => item.type === "start").sessionId;
    await act(async () => f.rendered.update(React.createElement(f.ChatComposer, { mode: "auto", onModeChange: () => {}, paired: false, active: false })));
    await act(async () => { old({ data: JSON.stringify({ type: "sentence", sessionId, utteranceId: "late", text: "迟到语音" }) }); f.useChat.getState().setBusy(false); await flush(); });
    assert.equal(f.requests.length, 0);
    assert.equal(f.posts.at(-1).type, "stop");
  } finally { await f.close(); }
});

componentTest("permission sheet and switching apps keep listening; each new estop and native screen-lock off stop it", async () => {
  const f = await fixture();
  try {
    await act(async () => f.button("开启持续语音输入").props.onClick());
    await f.event({ type: "state", status: "preparing", reason: "permission" });
    await act(async () => { f.context.document.hidden = true; f.context.document.dispatchEvent(new Event("visibilitychange")); });
    assert.equal(f.posts.at(-1).type, "start");
    f.context.document.hidden = false; await f.event({ type: "state", status: "listening" });
    await act(async () => f.useApp.setState({ state: { ...f.useApp.getState().state, estop: true } }));
    assert.equal(f.posts.at(-1).type, "stop");
    await f.start(); // Explicit voice start while stopped remains available for chat.
    await act(async () => f.useApp.setState({ state: { ...f.useApp.getState().state, estop: false } }));
    await act(async () => f.useApp.setState({ state: { ...f.useApp.getState().state, estop: true } }));
    assert.equal(f.posts.at(-1).type, "stop");
    await f.start();
    await act(async () => { f.context.document.hidden = true; f.context.document.dispatchEvent(new Event("visibilitychange")); });
    assert.equal(f.posts.at(-1).type, "start");
    await f.event({ type: "state", status: "off", message: "屏幕已锁定，语音已停止" });
    assert.equal(f.posts.at(-1).type, "stop");
  } finally { await f.close(); }
});

componentTest("unsafe failed voice plus queued text cannot become an unchanged new-ID retry", async () => {
  const gate = deferred(); const f = await fixture(() => gate.promise);
  try {
    await f.start(); await f.sentence("one", "可能部分执行的操作"); await f.sentence("two", "后续语音");
    await act(async () => { gate.resolve(response({ status: "completed", request_id: f.requests[0].body.request_id, result: { line: "", executed: [], dropped: [], error: "结果无法确认", retryable: false } })); await flush(); });
    const review = f.rendered.root.findAllByType("textarea").find((node) => node.props.id === "voice-review");
    assert.equal(review.props.value, "可能部分执行的操作\n后续语音");
    assert.equal(f.button("发送转写").props.disabled, true);
    await act(async () => review.props.onChange({ target: { value: "先告诉我设备现在的状态" } }));
    assert.equal(f.button("发送转写").props.disabled, false);
    assert.equal(f.requests.length, 1);
  } finally { await f.close(); }
});

componentTest("spoken emergency stop still sends immediately while a model reply is outstanding", async () => {
  const gate = deferred();
  const f = await fixture((url) => url === "/api/estop" ? response({ estop: true, sent: true }) : gate.promise);
  try {
    await f.start(); await f.enableSpeech(); await f.sentence("one", "等待回复"); await f.sentence("two", "待发内容"); await f.sentence("stop", "停止设备！");
    assert.deepEqual(f.requests.map((item) => item.url), ["/api/chat", "/api/estop"]);
    assert.equal(f.posts.at(-1).type, "stop");
    assert.equal(f.speechPosts.at(-1).type, "disable");
    await act(async () => { gate.resolve(complete(f.requests[0].body)); await flush(); });
    assert.equal(f.requests.length, 2);
  } finally { await f.close(); }
});

componentTest("playback is off by default and new HTTP/automatic AI text immediately replaces older speech", async () => {
  const f = await fixture();
  try {
    await act(async () => f.useChat.getState().push({ role: "ai", text: "开启前的历史" }));
    assert.equal(f.speechPosts.length, 0);
    await f.enableSpeech();
    await act(async () => {
      f.useChat.getState().push({ role: "sys", text: "系统提示" });
      f.useChat.getState().push({ role: "ai", text: "自动回合的新回复", actions: "A 强度 15" });
    });
    const first = f.speechPosts.find((item) => item.type === "speak");
    assert.equal(first.text, "自动回合的新回复");
    await f.draft("键盘发送");
    await act(async () => { f.rendered.root.findByType("form").props.onSubmit({ preventDefault() {} }); await flush(); });
    assert.equal(f.speechPosts.filter((item) => item.type === "speak").length, 2);
    assert.equal(f.speechPosts.filter((item) => item.type === "speak")[1].replace, true);
    await f.speechEvent({ status: "ready", utteranceId: first.utteranceId });
    assert.deepEqual(f.speechPosts.filter((item) => item.type === "speak").map((item) => item.text), ["自动回合的新回复", "收到"]);
    assert.equal(f.useChat.getState().messages.filter((item) => item.role === "ai").length, 3);
  } finally { await f.close(); }
});

componentTest("action-only HTTP placeholder is visible but never spoken", async () => {
  const f = await fixture(async (_url, body) => response({ status: "completed", request_id: body.request_id, result: { line: "", executed: [{ label: "A 强度 10", sent: true, command: { kind: "hold", channel: "A", value: 10 } }], dropped: [] } }));
  try {
    await f.enableSpeech(); await f.draft("测试操作回执");
    await act(async () => { f.rendered.root.findByType("form").props.onSubmit({ preventDefault() {} }); await flush(); });
    assert.equal(f.useChat.getState().messages.at(-1).text, "设备操作已处理。");
    assert.equal(f.speechPosts.filter((item) => item.type === "speak").length, 0);
  } finally { await f.close(); }
});

componentTest("playback blocks captured echo before native onStart and ignores replayed echo IDs after resume", async () => {
  const f = await fixture();
  try {
    await f.start(); await f.enableSpeech();
    await act(async () => f.useChat.getState().push({ role: "ai", text: "朗读的 AI 回复" }));
    const spoken = f.speechPosts.find((item) => item.type === "speak");
    await f.sentence("echo", "朗读的 AI 回复");
    assert.equal(f.requests.length, 0);
    await f.event({ type: "state", status: "listening", reason: "playback" });
    assert.equal(f.rendered.root.findAllByType("span").some((node) => String(node.props.children).startsWith("正在朗读 · 暂停识别")), true);
    await f.speechEvent({ status: "ready", utteranceId: spoken.utteranceId });
    await f.event({ type: "state", status: "listening" });
    await f.sentence("echo", "朗读的 AI 回复");
    assert.equal(f.requests.length, 0);
    await f.sentence("new-speech", "用户在朗读结束后说话");
    assert.equal(f.requests.length, 1);
  } finally { await f.close(); }
});

componentTest("microphone and playback toggles are independent and playback off immediately stops audio", async () => {
  const f = await fixture();
  try {
    await f.start(); await f.enableSpeech();
    await act(async () => f.button("停止语音输入").props.onClick());
    assert.equal(f.posts.at(-1).type, "stop");
    assert.equal(f.speechPosts.at(-1).type, "enable");
    assert.equal(f.button("关闭回复朗读").props["aria-pressed"], true);
    await f.start();
    await act(async () => f.useChat.getState().push({ role: "ai", text: "正在朗读" }));
    await act(async () => f.button("关闭回复朗读").props.onClick());
    assert.deepEqual(f.speechPosts.slice(-2).map((item) => item.type), ["stop", "disable"]);
    assert.equal(f.button("停止语音输入").props["aria-pressed"], true);
  } finally { await f.close(); }
});

componentTest("native duplex keeps meter, preview and final chat working during speech; interruption keeps playback enabled", async () => {
  const gate = deferred();
  const f = await fixture(async () => gate.promise);
  try {
    await f.draft("保留手动草稿"); await f.start(); await f.enableSpeech();
    await f.event({ type: "state", status: "listening", duplex: true });
    await act(async () => f.useChat.getState().push({ role: "ai", text: "正在朗读的旧回复" }));
    const old = f.speechPosts.find((item) => item.type === "speak");
    await f.event({ type: "level", level: 0.65 }); await f.event({ type: "preview", text: "用户正在插话" });
    assert.equal(f.button("停止语音输入").props.style["--voice-level"], 0.65);
    assert.equal(f.rendered.root.findByProps({ className: "android-voice-preview" }).props.children, "用户正在插话");
    await f.sentence("barge-in", "用户插话的最终句子");
    assert.equal(f.requests.length, 1); assert.equal(f.requests[0].body.message, "用户插话的最终句子");
    assert.equal(f.input().props.value, "保留手动草稿");
    await f.speechEvent({ type: "interrupted", utteranceId: old.utteranceId });
    assert.equal(f.button("关闭回复朗读").props["aria-pressed"], true);
    assert.equal(f.button("停止语音输入").props["aria-pressed"], true);
    await act(async () => { gate.resolve(complete(f.requests[0].body)); await flush(); });
    assert.deepEqual(f.speechPosts.filter((item) => item.type === "speak").map((item) => item.text), ["正在朗读的旧回复", "收到"]);
    await f.event({ type: "state", status: "listening", duplex: true, reason: "playback" });
    await f.event({ type: "preview", text: "应拦截的回声" }); await f.sentence("echo-route", "应拦截的回声");
    assert.equal(f.requests.length, 1);
    assert.equal(f.rendered.root.findByProps({ className: "android-voice-preview" }).props.children, "正在朗读 · 暂停识别");
  } finally { await f.close(); }
});

componentTest("role/session reset, history clear, and cached-page navigation cancel playback without reading old queues", async () => {
  const f = await fixture();
  try {
    await f.enableSpeech();
    await act(async () => { f.useChat.getState().push({ role: "ai", text: "正在播" }); f.useChat.getState().push({ role: "ai", text: "旧角色待播" }); });
    await act(async () => f.useApp.setState({ state: { ...f.useApp.getState().state, role: "另一个角色" } }));
    assert.equal(f.speechPosts.at(-1).type, "disable");
    await f.enableSpeech();
    assert.equal(f.speechPosts.filter((item) => item.type === "speak").length, 2);
    await act(async () => f.useChat.getState().clear());
    assert.equal(f.speechPosts.at(-1).type, "disable");
    await f.enableSpeech();
    await act(async () => f.rendered.update(React.createElement(f.ChatComposer, { mode: "auto", onModeChange: () => {}, paired: false, active: false })));
    assert.equal(f.speechPosts.at(-1).type, "disable");
  } finally { await f.close(); }
});

componentTest("a unavailable native TTS bridge reports an error without invoking browser/network speech", async () => {
  const f = await fixture();
  try {
    delete f.context.window.CoyoteSpeech;
    await act(async () => f.button("开启回复朗读").props.onClick());
    assert.equal(f.rendered.root.findAllByProps({ role: "alert" }).some((node) => String(node.props.children).includes("未提供回复朗读")), true);
    assert.equal(f.requests.length, 0);
    assert.equal(f.button("开启回复朗读").props["aria-pressed"], false);
  } finally { await f.close(); }
});

componentTest("archive hydration never speaks history and live receipt redelivery speaks each stable ID once", async () => {
  const f = await fixture();
  try {
    const item = (id, text) => ({ id, conversation_id: "one", role: "ai", text, created_at: Number(id), executed: [], dropped: [] });
    await act(async () => { f.useApp.setState({ state: { ...f.useApp.getState().state, conversation_id: "one" } }); f.useChat.getState().beginConversation("one"); f.useChat.getState().hydrate("one", [item("1", "旧消息")], false); });
    await f.enableSpeech();
    await act(async () => f.useChat.getState().ingest("one", [item("2", "新回复")]));
    await act(async () => f.useChat.getState().ingest("one", [item("2", "新回复")]));
    assert.deepEqual(f.speechPosts.filter((p) => p.type === "speak").map((p) => p.text), ["新回复"]);
    await act(async () => f.useChat.getState().hydrate("one", [item("1", "旧消息"), item("2", "新回复"), item("3", "重连补回的历史")], false));
    assert.equal(f.speechPosts.at(-1).type, "disable");
    assert.equal(f.speechPosts.filter((p) => p.type === "speak").length, 1);
    await f.enableSpeech(); await f.start();
    await act(async () => f.useApp.setState({ state: { ...f.useApp.getState().state, conversation_id: "two" } }));
    assert.equal(f.speechPosts.at(-1).type, "disable"); assert.equal(f.posts.at(-1).type, "stop");
    assert.equal(f.requests.length, 0);
  } finally { await f.close(); }
});
