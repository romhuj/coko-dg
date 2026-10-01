import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/replySpeech.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const { ReplySpeech, SPEECH_VOICES, resolveSpeechVoice } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const ai = (text, extra = {}) => ({ role: "ai", text, ...extra });
function fixture() {
  const posts = [];
  let id = 0, allowed = true;
  const bridge = { onmessage: null, postMessage: (raw) => posts.push(JSON.parse(raw)) };
  const speech = new ReplySpeech({ bridge: () => bridge, createId: () => `id-${++id}`, allowed: () => allowed, changed: () => {} });
  const event = (data) => bridge.onmessage?.({ data: JSON.stringify({ type: "state", sessionId: speech.snapshot.sessionId, ...data }) });
  const enable = (history = []) => { speech.enable(history); event({ status: "ready" }); };
  return { speech, posts, bridge, event, enable, allow: (value) => { allowed = value; }, spoken: () => posts.filter((item) => item.type === "speak") };
}

test("playback starts off and enable does not replay history, user/sys text, or action-only placeholders", () => {
  const f = fixture(), history = ai("开启前的回复");
  f.speech.accept([ai("关闭时的回复")]); assert.equal(f.posts.length, 0);
  f.enable([history]);
  f.speech.accept([history, { role: "user", text: "用户消息" }, { role: "sys", text: "错误提示" }, ai("设备操作已处理。", { speechText: "", actions: "A 强度 10" }), ai("  ")]);
  assert.equal(f.spoken().length, 0);
  f.speech.accept([ai("新的 AI 回复", { actions: "A 强度 10" })]);
  assert.equal(f.spoken()[0].text, "新的 AI 回复");
});

test("new AI replies replace current speech immediately; old callbacks and duplicate objects cannot replay it", () => {
  const f = fixture(), first = ai("相同文字"), second = ai("相同文字");
  f.enable(); f.speech.accept([first, first]);
  assert.equal(f.spoken().length, 1); assert.equal(f.speech.snapshot.queued, 0);
  const original = f.spoken()[0].utteranceId;
  f.event({ status: "speaking", utteranceId: original });
  assert.equal(f.speech.snapshot.status, "speaking");
  f.speech.accept([second]);
  assert.equal(f.spoken().length, 2);
  assert.equal(f.spoken()[1].replace, true);
  assert.notEqual(f.spoken()[0].utteranceId, f.spoken()[1].utteranceId);
  const current = f.spoken()[1].utteranceId;
  f.event({ status: "ready" }); f.event({ status: "ready", utteranceId: original });
  f.event({ status: "speaking", utteranceId: original }); f.event({ status: "error", utteranceId: original });
  f.event({ type: "interrupted", utteranceId: original });
  assert.equal(f.speech.snapshot.utteranceId, current);
  f.speech.accept([first, second]);
  assert.equal(f.spoken().length, 2);
  f.event({ status: "ready", utteranceId: current });
  assert.equal(f.speech.snapshot.utteranceId, null);
  assert.equal(f.speech.snapshot.status, "ready");
});

test("replies received during initialization retain only the newest before ready", () => {
  const f = fixture(); f.speech.enable([]); f.speech.accept([ai("初始化期间旧回复"), ai("初始化期间的新回复")]);
  assert.equal(f.spoken().length, 0); assert.equal(f.speech.snapshot.queued, 1);
  f.event({ status: "ready" }); assert.equal(f.spoken().length, 1);
  assert.equal(f.spoken()[0].text, "初始化期间的新回复");
});

test("disable stops audio, clears pending replies, and rejects late events from the old session", () => {
  const f = fixture(); f.enable(); f.speech.accept([ai("正在播放"), ai("待播")]);
  const old = f.bridge.onmessage, sessionId = f.speech.snapshot.sessionId, utteranceId = f.speech.snapshot.utteranceId;
  f.speech.disable();
  assert.deepEqual(f.posts.slice(-2).map((item) => item.type), ["stop", "disable"]);
  assert.equal(f.speech.snapshot.queued, 0);
  f.enable();
  old({ data: JSON.stringify({ type: "state", sessionId, status: "ready", utteranceId }) });
  assert.equal(f.spoken().length, 1);
});

test("a batch of new replies reads only its newest item without accumulating or disabling playback", () => {
  const f = fixture(); f.enable(); f.speech.accept(Array.from({ length: 7 }, (_, i) => ai(`第${i}条回复`)));
  assert.equal(f.spoken().length, 1);
  assert.equal(f.spoken()[0].text, "第6条回复");
  assert.equal(f.speech.snapshot.status, "speaking");
  assert.equal(f.speech.snapshot.queued, 0);
  assert.equal(f.posts.at(-1).type, "speak");
});

test("matching user interruption clears old audio but keeps playback enabled for the next reply", () => {
  const f = fixture(); f.enable(); f.speech.accept([ai("正在朗读")]);
  const session = f.speech.snapshot.sessionId, old = f.speech.snapshot.utteranceId;
  f.event({ type: "interrupted", utteranceId: old });
  assert.equal(f.speech.snapshot.sessionId, session);
  assert.equal(f.speech.snapshot.status, "ready");
  assert.equal(f.speech.snapshot.queued, 0);
  assert.equal(f.speech.snapshot.utteranceId, null);
  f.event({ status: "speaking", utteranceId: old }); f.event({ status: "ready", utteranceId: old });
  assert.equal(f.speech.snapshot.status, "ready");
  f.speech.accept([ai("回应用户插话的新回复")]);
  assert.equal(f.spoken().length, 2);
  assert.equal(f.spoken()[1].text, "回应用户插话的新回复");
  f.event({ type: "interrupted", sessionId: "stale-session", utteranceId: f.speech.snapshot.utteranceId });
  assert.equal(f.speech.snapshot.status, "speaking");
});

test("native errors and cancellation stop the queue without automatic retries", () => {
  const f = fixture(); f.enable(); f.speech.accept([ai("第一条"), ai("第二条")]);
  f.event({ status: "error", utteranceId: f.speech.snapshot.utteranceId, message: "没有可用的系统语音" });
  assert.equal(f.spoken().length, 1); assert.equal(f.speech.snapshot.status, "error");
  f.enable(); f.speech.accept([ai("新的回复")]);
  f.event({ status: "off", utteranceId: "native-last-utterance", message: "屏幕锁定" });
  assert.equal(f.speech.snapshot.status, "off"); assert.equal(f.speech.snapshot.utteranceId, null);
});

test("context cancellation prevents the next pending reply crossing the bridge", () => {
  const f = fixture(); f.enable(); f.speech.accept([ai("第一条"), ai("第二条")]);
  const utteranceId = f.speech.snapshot.utteranceId;
  f.allow(false); f.event({ status: "ready", utteranceId });
  assert.equal(f.spoken().length, 1); assert.equal(f.speech.snapshot.status, "off");
});

test("voice selection exposes approved voices and defaults legacy roles to system voice", () => {
  assert.deepEqual(SPEECH_VOICES.map((voice) => voice.id), ["system-default", "kokoro-zf_001", "kokoro-zm_010", "melo-zh"]);
  assert.equal(resolveSpeechVoice("system-default").id, "system-default");
  assert.equal(resolveSpeechVoice("unreviewed-voice").id, "system-default");
  const f = fixture();
  f.speech.enable([], "kokoro-zm_010");
  assert.equal(f.posts.at(-1).voiceId, "kokoro-zm_010");
  f.speech.disable(); f.speech.enable([]);
  assert.equal(f.posts.at(-1).voiceId, "system-default");
});
