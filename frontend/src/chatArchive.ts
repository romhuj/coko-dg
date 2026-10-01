import { api } from "./api";
import { useApp, useChat, type ChatMsg } from "./store";
import type { ArchivedChatMessage, ConversationPage } from "./types";
import { labels } from "./commands";

let refreshGeneration = 0;
const mapMessages = (messages: ArchivedChatMessage[]): ChatMsg[] => messages.map((message) => ({ ...message,
  actions: labels({ executed: message.executed ?? [], dropped: message.dropped ?? [] }) }));

export function acceptArchivedMessages(conversationId: string, messages: ArchivedChatMessage[]): void {
  if (useApp.getState().state?.conversation_id !== conversationId) return;
  useChat.getState().beginConversation(conversationId);
  useChat.getState().ingest(conversationId, mapMessages(messages));
}

export function restoreConversation(page: ConversationPage): void {
  refreshGeneration++;
  window.dispatchEvent(new Event("coyote:voice-stop"));
  const chat = useChat.getState();
  chat.beginConversation(page.conversation_id);
  useChat.getState().hydrate(page.conversation_id, mapMessages(page.messages), page.has_more);
}

export async function refreshConversation(older = false): Promise<void> {
  const id = useApp.getState().state?.conversation_id;
  if (!id) return;
  const token = ++refreshGeneration;
  useChat.getState().beginConversation(id);
  const before = older ? useChat.getState().messages.find((message) => message.id)?.id : undefined;
  useChat.setState({ historyLoading: true, historyError: "" });
  try {
    const page = await api.conversationMessages(id, before);
    if (token !== refreshGeneration || useApp.getState().state?.conversation_id !== id || page.conversation_id !== id) return;
    if (older) useChat.getState().prependHistory(id, mapMessages(page.messages), page.has_more);
    else useChat.getState().hydrate(id, mapMessages(page.messages), page.has_more);
  } catch (error) {
    if (token === refreshGeneration && useApp.getState().state?.conversation_id === id) {
      useChat.setState({ historyLoading: false, historyError: error instanceof Error ? error.message : "聊天记录加载失败" });
    }
  }
}

export function conversationSwitchBlocked(): boolean {
  const chat = useChat.getState(), state = useApp.getState().state;
  return chat.busy || chat.awaitingConfirmation || chat.historyLoading || !!state?.turn_busy || (state?.pending_chat ?? 0) > 0;
}
