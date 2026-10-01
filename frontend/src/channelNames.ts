import type { ChannelDevice } from "./types";

export function channelLabel(channel: "A" | "B", devices?: Partial<Record<"A" | "B", ChannelDevice>>): string {
  const name = devices?.[channel]?.name?.trim();
  return name && name !== `${channel} 通道` && name !== `通道 ${channel}` && name !== channel ? `${channel} · ${name}` : `${channel} 通道`;
}
