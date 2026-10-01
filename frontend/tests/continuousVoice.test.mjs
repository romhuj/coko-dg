import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/continuousVoice.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const { ContinuousVoice, isVoiceEmergencyStop } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
function fixture(options = {}) {
  const posts = [], sent = [], reviews = [], stops = [];
  let allowed = true, blocked = false, session = 0;
  const bridge = { onmessage: null, postMessage: (data) => posts.push(JSON.parse(data)) };
  const voice = new ContinuousVoice({
    bridge: () => bridge,
    createSessionId: () => `voice-${++session}`,
    allowed: () => allowed,
    blocked: () => blocked,
    captureBlocked: options.captureBlocked,
    submit: async (sentence) => { sent.push(sentence); return options.submit ? options.submit(sentence) : { status: "sent" }; },
    changed: () => {},
    review: (texts, message) => reviews.push({ texts, message }),
    emergencyStop: async () => { stops.push("estop"); },
  });
  const event = (data) => bridge.onmessage?.({ data: JSON.stringify({ sessionId: voice.snapshot.sessionId, ...data }) });
const sentence = (utteranceId, text) => event({ type: "sentence", utteranceId, text });
  const start = () => { voice.start(); event({ type: "state", status: "listening" }); };
  return { voice, posts, sent, reviews, stops, bridge, event, sentence, start, block: (value) => { blocked = value; }, allow: (value) => { allowed = value; } };
}

test("preview and finite levels update only the active listening session and never send", () => {
  const f = fixture(); f.voice.start();
  f.event({ type: "level", level: 1 }); f.event({ type: "preview", text: "准备中忽略" });
  assert.equal(f.voice.snapshot.level, 0); assert.equal(f.voice.snapshot.preview, "");
  f.event({ type: "state", status: "listening" });
  f.event({ type: "level", level: 0.6 }); f.event({ type: "preview", text: "当前转写" });
  assert.equal(f.voice.snapshot.level, 0.6); assert.equal(f.voice.snapshot.preview, "当前转写");
  assert.equal(f.sent.length, 0); assert.equal(f.voice.snapshot.queued, 0);
  f.event({ type: "level", level: "0.9" }); f.event({ type: "level", level: null });
  assert.equal(f.voice.snapshot.level, 0.6);
  f.event({ type: "level", level: 9 }); assert.equal(f.voice.snapshot.level, 1);
  f.event({ type: "level", level: -3 }); assert.equal(f.voice.snapshot.level, 0);
  f.event({ type: "preview", text: "前缀" + "尾".repeat(8000) }); assert.equal(f.voice.snapshot.preview, "尾".repeat(8000));
  f.event({ type: "preview", text: "" }); assert.equal(f.voice.snapshot.preview, "");
  const sessionId = f.voice.snapshot.sessionId, late = f.bridge.onmessage;
  f.voice.stop(); f.start();
  late({ data: JSON.stringify({ type: "preview", sessionId, text: "旧会话转写" }) });
  late({ data: JSON.stringify({ type: "level", sessionId, level: 1 }) });
  assert.equal(f.voice.snapshot.preview, ""); assert.equal(f.voice.snapshot.level, 0);
});

test("playback pause and stopping clear feedback; late preview cannot display application audio", () => {
  let blocked = false;
  const f = fixture({ captureBlocked: () => blocked }); f.start();
  f.event({ type: "preview", text: "用户语音" }); f.event({ type: "level", level: 0.7 });
  f.event({ type: "state", status: "listening", reason: "playback" });
  assert.equal(f.voice.snapshot.preview, ""); assert.equal(f.voice.snapshot.level, 0);
  f.event({ type: "preview", text: "朗读回声" }); f.event({ type: "level", level: 1 });
  assert.equal(f.voice.snapshot.preview, ""); assert.equal(f.voice.snapshot.level, 0);
  f.event({ type: "state", status: "listening" }); blocked = true;
  f.event({ type: "preview", text: "尚未发来暂停事件的朗读" });
  assert.equal(f.voice.snapshot.preview, "");
  blocked = false; f.event({ type: "preview", text: "新句子" }); f.event({ type: "level", level: 0.8 });
  f.voice.stop();
  assert.equal(f.voice.snapshot.preview, ""); assert.equal(f.voice.snapshot.level, 0);
});

test("confirmed duplex permits live feedback and final sentences during playback, explicit fallback still blocks", async () => {
  const f = fixture({ captureBlocked: () => true }); f.start();
  f.event({ type: "preview", text: "未知能力时的回声" }); f.sentence("unknown", "回声");
  assert.equal(f.sent.length, 0); assert.equal(f.voice.snapshot.preview, "");
  f.event({ type: "state", status: "listening", duplex: true });
  f.event({ type: "level", level: 0.8 }); f.event({ type: "preview", text: "用户正在插话" });
  assert.equal(f.voice.snapshot.level, 0.8); assert.equal(f.voice.snapshot.preview, "用户正在插话");
  f.sentence("barge-in", "用户插话的完整句子"); await flush();
  assert.deepEqual(f.sent.map((item) => item.text), ["用户插话的完整句子"]);
  f.event({ type: "state", status: "listening", duplex: true, reason: "playback" });
  f.event({ type: "preview", text: "切换音频路线期间的回声" }); f.sentence("route-echo", "回声");
  assert.equal(f.voice.snapshot.preview, ""); assert.equal(f.voice.snapshot.level, 0);
  assert.equal(f.sent.length, 1);
  f.event({ type: "state", status: "listening", duplex: false });
  f.sentence("downgraded", "降级回声"); assert.equal(f.sent.length, 1);
  f.voice.stop(); f.start();
  assert.equal(f.voice.snapshot.duplex, false);
  f.event({ type: "state", status: "listening", duplex: "true" });
  assert.equal(f.voice.snapshot.duplex, false);
});

test("only final native sentences send; duplicate IDs are ignored but repeated words with a new ID send", async () => {
  const f = fixture();
  f.start();
  f.event({ type: "partial", utteranceId: "partial", text: "中间结果" });
  f.sentence("one", "  你好  ");
  f.sentence("one", "你好");
  f.sentence("two", "你好");
  await flush();
  assert.deepEqual(f.sent.map((item) => [item.utteranceId, item.text]), [["one", "你好"], ["two", "你好"]]);
});

test("continuous capture queues behind a busy typed request and drains serially", async () => {
  const gate = deferred();
  const f = fixture({ submit: () => gate.promise });
  f.block(true); f.start();
  f.sentence("one", "第一句"); f.sentence("two", "第二句");
  assert.equal(f.sent.length, 0);
  assert.equal(f.voice.snapshot.queued, 2);
  f.block(false); f.voice.notifyReady(); f.voice.notifyReady();
  assert.equal(f.sent.length, 1);
  assert.equal(f.voice.snapshot.queued, 1);
  gate.resolve({ status: "sent" }); await flush();
  assert.deepEqual(f.sent.map((item) => item.text), ["第一句", "第二句"]);
  assert.equal(f.voice.snapshot.queued, 0);
});

test("overflow pauses capture and preserves all bounded queued transcriptions plus the triggering sentence", () => {
  const f = fixture(); f.block(true); f.start();
  for (let i = 1; i <= 6; i++) f.sentence(String(i), `第${i}句`);
  assert.equal(f.voice.snapshot.status, "error");
  assert.equal(f.voice.snapshot.queued, 0);
  assert.equal(f.sent.length, 0);
  assert.equal(f.reviews[0].texts.length, 6);
  assert.equal(f.posts.at(-1).type, "stop");
});

test("stop, navigation, and session replacement reject copied late callbacks and clear unsent speech", async () => {
  const f = fixture(); f.block(true); f.start();
  const oldHandler = f.bridge.onmessage, oldSession = f.voice.snapshot.sessionId;
  f.sentence("one", "过期内容");
  f.voice.stop();
  oldHandler({ data: JSON.stringify({ type: "sentence", sessionId: oldSession, utteranceId: "late", text: "迟到内容" }) });
  f.start();
  oldHandler({ data: JSON.stringify({ type: "sentence", sessionId: oldSession, utteranceId: "later", text: "更迟内容" }) });
  f.allow(false); f.sentence("new", "离开页面后内容");
  f.block(false); f.voice.notifyReady(); await flush();
  assert.equal(f.sent.length, 0);
  assert.equal(f.voice.snapshot.status, "off");
});

test("failed send is never automatically retried and preserves the failed sentence and remaining queue", async () => {
  const gate = deferred(); const f = fixture({ submit: () => gate.promise });
  f.start(); f.sentence("one", "失败的句子"); f.sentence("two", "后续句子");
  gate.resolve({ status: "failed", message: "模型暂时不可用" }); await flush();
  f.voice.notifyReady(); await flush();
  assert.equal(f.sent.length, 1);
  assert.equal(f.voice.snapshot.status, "error");
  assert.deepEqual(f.reviews[0].texts, ["失败的句子", "后续句子"]);
});

test("uncertain receipt stops recognition and preserves only unsent speech; pending request is confirmed separately", async () => {
  const gate = deferred(); const f = fixture({ submit: () => gate.promise });
  f.start(); f.sentence("one", "已发但回执丢失"); f.sentence("two", "尚未发送");
  gate.resolve({ status: "uncertain", message: "结果待确认" }); await flush();
  f.voice.notifyReady(); await flush();
  assert.equal(f.sent.length, 1);
  assert.deepEqual(f.reviews[0].texts, ["尚未发送"]);
  assert.equal(f.voice.snapshot.status, "error");
});

test("blocked shared sender waits for a readiness change instead of spinning or losing the sentence", async () => {
  let ready = false;
  const f = fixture({ submit: async () => ({ status: ready ? "sent" : "blocked" }) });
  f.start(); f.sentence("one", "保留这句"); await flush();
  assert.equal(f.sent.length, 1);
  assert.equal(f.voice.snapshot.queued, 1);
  ready = true; f.voice.notifyReady(); await flush();
  assert.equal(f.sent.length, 2);
  assert.equal(f.voice.snapshot.queued, 0);
});

test("permission and download states do not send early sentences and cancellation stops native work", () => {
  const f = fixture(); f.voice.start();
  f.event({ type: "state", status: "preparing", reason: "permission", message: "请允许麦克风权限" });
  assert.equal(f.voice.snapshot.reason, "permission");
  f.sentence("early", "尚未开始监听");
  f.event({ type: "state", status: "downloading", progress: 37 });
  assert.equal(f.voice.snapshot.progress, 37);
  assert.equal(f.voice.snapshot.reason, undefined);
  f.voice.stop();
  assert.equal(f.posts.at(-1).type, "stop");
  assert.equal(f.sent.length, 0);
});

test("native error preserves queued transcriptions; native off cancels them", () => {
  const f = fixture(); f.block(true); f.start(); f.sentence("one", "保留待发");
  f.event({ type: "state", status: "error", message: "麦克风不可用" });
  assert.deepEqual(f.reviews[0].texts, ["保留待发"]);
  f.start(); f.sentence("two", "后台应取消"); f.event({ type: "state", status: "off" });
  assert.equal(f.voice.snapshot.queued, 0);
  assert.equal(f.reviews.length, 1);
});

test("an already submitted sentence still retains its failure after listening is canceled", async () => {
  const gate = deferred(); const f = fixture({ submit: () => gate.promise });
  f.start(); f.sentence("one", "已经提交的语音"); f.sentence("two", "取消的待发语音");
  f.voice.stop(); gate.resolve({ status: "failed", message: "模型失败" }); await flush();
  assert.deepEqual(f.reviews[0].texts, ["已经提交的语音"]);
  assert.equal(f.sent.length, 1);
});

test("strict emergency-stop sentences bypass a blocked queue and cancel capture immediately", async () => {
  const f = fixture(); f.block(true); f.start();
  f.sentence("one", "普通操作"); f.sentence("stop", "急停！");
  await flush();
  assert.equal(f.stops.length, 1);
  assert.equal(f.sent.length, 0);
  assert.equal(f.voice.snapshot.status, "off");
  assert.equal(f.voice.snapshot.queued, 0);
});

test("stop matching never treats negation, quotes, or ordinary instructions as direct device commands", () => {
  for (const phrase of ["急停", "停止设备。", "STOP!", " estop！ "]) assert.equal(isVoiceEmergencyStop(phrase), true, phrase);
  for (const phrase of ["不要急停", "不要停止设备", "他说急停", "‘急停’", "\"stop\"", "stop please", "急停吗？", "增强强度", "停止自动运行"]) assert.equal(isVoiceEmergencyStop(phrase), false, phrase);
});
