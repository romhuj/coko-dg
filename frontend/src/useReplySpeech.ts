import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ReplySpeech, resolveSpeechVoice, type SpeechBridge, type SpeechSnapshot } from "./replySpeech";
import { useApp, useChat } from "./store";

declare global { interface Window { CoyoteSpeech?: SpeechBridge } }

export function useReplySpeech(options: { enabled: boolean; active: boolean; contextKey: string; estop: boolean }) {
  const latest = useRef(options);
  latest.current = options;
  const startedContext = useRef("");
  const [state, setState] = useState<SpeechSnapshot>({ status: "off", sessionId: null, utteranceId: null, queued: 0, message: "" });
  const controller = useRef<ReplySpeech | null>(null);
  if (!controller.current) controller.current = new ReplySpeech({
    bridge: () => window.CoyoteSpeech,
    createId: () => crypto.randomUUID(),
    allowed: () => latest.current.enabled && latest.current.active && latest.current.contextKey === startedContext.current,
    changed: setState,
  });
  const speech = controller.current;
  useLayoutEffect(() => {
    if (!options.enabled || !options.active || options.contextKey !== startedContext.current) speech.disable();
  }, [speech, options.enabled, options.active, options.contextKey]);
  useEffect(() => {
    const chat = useChat.subscribe((next, previous) => {
      if (next.messages === previous.messages) return;
      if (next.historyRevision !== previous.historyRevision) { speech.disable(); return; }
      if (next.messages.length === 0) { speech.disable(); return; }
      const appendOnly = next.messages.length >= previous.messages.length && previous.messages.every((message) => next.messages.some((item) => message.id ? item.id === message.id : item === message));
      if (!appendOnly) { speech.disable(); return; }
      speech.accept(next.messages.filter((message) => !previous.messages.some((item) => message.id ? item.id === message.id : item === message)));
    });
    // Cancel synchronously when state changes, before a following WS reply could enter the old voice queue.
    const app = useApp.subscribe((next, previous) => {
      const current = next.state, old = previous.state;
      if (current?.chat_session_id !== old?.chat_session_id || current?.conversation_id !== old?.conversation_id || current?.role !== old?.role || current?.profile !== old?.profile || (current?.estop && !old?.estop)) speech.disable();
    });
    const stop = () => speech.disable();
    const estop = (event: Event) => { if ((event.target as Element | null)?.closest?.("[data-coyote-estop]")) stop(); };
    document.addEventListener("click", estop, true);
    window.addEventListener("pagehide", stop);
    window.addEventListener("coyote:voice-stop", stop);
    return () => {
      chat(); app();
      document.removeEventListener("click", estop, true);
      window.removeEventListener("pagehide", stop);
      window.removeEventListener("coyote:voice-stop", stop);
      speech.disable();
    };
  }, [speech]);
  return {
    ...state,
    disable: () => speech.disable(),
    // Consult the controller synchronously, before a React render or native onStart callback.
    isPlaying: () => !!speech.snapshot.utteranceId,
    enable: () => {
      startedContext.current = latest.current.contextKey;
      const app = useApp.getState().state;
      const voice = app?.roles?.find((role) => role.name === app.role)?.voiceId;
      speech.enable(useChat.getState().messages, resolveSpeechVoice(voice).id);
    },
  };
}
