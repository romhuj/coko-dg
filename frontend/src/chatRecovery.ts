export type FrozenChatRequest = { requestId: string; message: string; draft: string; mode: "auto" | "text"; preferredPattern?: string; conversationId?: string; origin?: "voice"; keyboardDraft?: string };
const PENDING_CHAT_KEY = "coyote.pending-chat.v1";
export function readPendingChat(): FrozenChatRequest | null {
  try {
    const request = JSON.parse(sessionStorage.getItem(PENDING_CHAT_KEY) || "null");
    if (!request || typeof request.requestId !== "string" || !request.requestId || request.requestId.length > 180
      || typeof request.message !== "string" || !request.message || request.message.length > 8000
      || typeof request.draft !== "string" || request.draft.length > 8000 || request.draft.trim() !== request.message
      || !["auto", "text"].includes(request.mode)
      || (request.preferredPattern !== undefined && typeof request.preferredPattern !== "string")
      || (request.conversationId !== undefined && (typeof request.conversationId !== "string" || !request.conversationId || request.conversationId.length > 100))
      || (request.origin !== undefined && request.origin !== "voice")
      || (request.keyboardDraft !== undefined && (typeof request.keyboardDraft !== "string" || request.keyboardDraft.length > 8000))) return null;
    return { requestId: request.requestId, message: request.message, draft: request.draft, mode: request.mode, preferredPattern: request.preferredPattern,
      ...(request.conversationId ? { conversationId: request.conversationId } : {}),
      ...(request.origin === "voice" ? { origin: "voice" } : {}),
      ...(request.keyboardDraft !== undefined ? { keyboardDraft: request.keyboardDraft } : {}) };
  } catch { return null; }
}
export function rememberPendingChat(request: FrozenChatRequest): void {
  try { sessionStorage.setItem(PENDING_CHAT_KEY, JSON.stringify(request)); } catch { /* In-memory retry still preserves the request. */ }
}
export function forgetPendingChat(requestId?: string): void {
  try {
    if (readPendingChat()?.requestId === requestId) sessionStorage.removeItem(PENDING_CHAT_KEY);
  } catch { /* Storage may be disabled. */ }
}

/** An unavailable/partially executed receipt cannot become a new identical action request. */
export function blocksUnsafeRetry(draft: string, blockedMessage: string | null): boolean {
  return blockedMessage !== null && draft.trim() === blockedMessage;
}
