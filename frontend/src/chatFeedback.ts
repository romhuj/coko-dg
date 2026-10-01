import type { ExecutedCommand, ManualResult } from "./types";

const number = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** Only normalized, actually sent commands supply action chips. Never read raw model actions. */
export function commandLabel(command: ExecutedCommand | undefined): string | null {
  if (!command || typeof command !== "object") return null;
  const channel = command.channel === "A" || command.channel === "B" ? command.channel : "A/B";
  switch (command.kind) {
    case "hold":
      return number(command.value) ? `${channel} 强度 ${command.value}` : null;
    case "add":
      if (!number(command.delta)) return null;
      return command.delta === 0 ? `${channel} 强度保持不变`
        : `${channel} ${command.delta > 0 ? "增加" : "减少"} ${Math.abs(command.delta)}`;
    case "temp":
      return number(command.value) && number(command.duration_s)
        ? `${channel} 强度 ${command.value} · ${command.duration_s} 秒` : null;
    case "pulse":
      return command.pattern && number(command.duration_s)
        ? `${channel} ${command.pattern} ${command.duration_s} 秒` : null;
    case "pulse_hold":
      return command.pattern ? `${channel} ${command.pattern} 循环` : null;
    case "zero":
      return `${channel} 强度 0`;
    case "clear":
      return `${channel} 停止并清零`;
    case "stop":
      return "A/B 停止并清零";
    default:
      return null;
  }
}

export function chatFeedback(result: Partial<ManualResult> | null | undefined): {
  sent: string[];
  other: { label: string; state: "unsent" | "unavailable" }[];
} {
  const sent: string[] = [];
  const other: { label: string; state: "unsent" | "unavailable" }[] = [];
  for (const item of result?.executed ?? []) {
    if (item.sent !== true || (item.status && !["confirmed", "sent"].includes(item.status))) {
      const fallback = item.status === "unchanged" ? "强度保持不变，无需发送" : item.status === "simulated" ? "模拟操作，未向设备发送" : item.status === "failed" ? "设备操作失败" : "设备未确认执行，请检查官方 App";
      other.push({ label: item.reason || fallback, state: "unsent" });
      continue;
    }
    const label = commandLabel(item.command);
    if (label) sent.push(label);
    else other.push({ label: "已发送，命令详情不可用", state: "unavailable" });
  }
  for (const item of result?.dropped ?? []) {
    other.push({ label: item.reason || "操作未发送", state: "unsent" });
  }
  return { sent, other };
}
