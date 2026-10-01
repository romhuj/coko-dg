import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/chatFeedback.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const { chatFeedback, commandLabel } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("green command chips use clamped receipt values, never raw model action or stale label", () => {
  const result = chatFeedback({ executed: [{ sent: true, label: "A 强度 180", action: { value: 180 }, command: { kind: "hold", channel: "A", value: 54 } }], dropped: [] });
  assert.deepEqual(result, { sent: ["A 强度 54"], other: [] });
});
test("unsent and dropped commands are never green", () => {
  const result = chatFeedback({ executed: [{ sent: false, reason: "设备未连接", command: { kind: "hold", channel: "A", value: 54 } }, { sent: "true", reason: "未确认", command: { kind: "clear" } }], dropped: [{ reason: "超过上限" }] });
  assert.deepEqual(result.sent, []);
  assert.deepEqual(result.other.map((item) => item.state), ["unsent", "unsent", "unsent"]);
});
test("actual delta is used instead of final absolute value", () => {
  assert.equal(commandLabel({ kind: "add", channel: "B", value: 54, delta: 15 }), "B 增加 15");
  assert.equal(commandLabel({ kind: "add", channel: "A", value: 0, delta: -6 }), "A 减少 6");
  assert.equal(commandLabel({ kind: "add", channel: "A", value: 54, delta: 0 }), "A 强度保持不变");
});
test("wave, temporary strength and full stop retain actual command semantics", () => {
  assert.equal(commandLabel({ kind: "pulse_hold", channel: "A", pattern: "挤压" }), "A 挤压 循环");
  assert.equal(commandLabel({ kind: "pulse", channel: "B", pattern: "挤压", duration_s: 2.5 }), "B 挤压 2.5 秒");
  assert.equal(commandLabel({ kind: "temp", channel: "A", value: 0, duration_s: 4 }), "A 强度 0 · 4 秒");
  assert.equal(commandLabel({ kind: "clear", channel: "B" }), "B 停止并清零");
  assert.equal(commandLabel({ kind: "stop" }), "A/B 停止并清零");
});
test("missing or malformed command metadata stays neutral", () => {
  const result = chatFeedback({ executed: [{ sent: true, label: "A 强度 999" }, { sent: true, command: { kind: "hold", channel: "A", value: NaN } }] });
  assert.deepEqual(result.sent, []);
  assert.equal(result.other.length, 2);
  assert.ok(result.other.every((item) => item.state === "unavailable" && !item.label.includes("999")));
});

test("RPC failures and unconfirmed writes cannot become green; implicit waveform stays a separate chip", () => {
  const result = chatFeedback({ executed: [
    { sent: true, status: "sent", source: "default_wave", command: { kind: "pulse_hold", channel: "A", pattern: "挤压" } },
    { sent: true, status: "confirmed", command: { kind: "hold", channel: "A", value: 56 } },
    ...["failed", "unconfirmed", "simulated", "unchanged"].map((status) => ({ sent: true, status, command: { kind: "hold", channel: "B", value: 99 } })),
  ] });
  assert.deepEqual(result.sent, ["A 挤压 循环", "A 强度 56"]);
  assert.equal(result.other.length, 4);
});
