import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ContinuousVoice, type VoiceBridge, type VoiceSentence, type VoiceSendResult, type VoiceSnapshot } from "./continuousVoice";

declare global { interface Window { CoyoteVoice?: VoiceBridge } }

export function useContinuousVoice(options: {
  enabled: boolean;
  active: boolean;
  contextKey: string;
  estop: boolean;
  busy: boolean;
  blocked: () => boolean;
  captureBlocked?: () => boolean;
  submit: (sentence: VoiceSentence) => Promise<VoiceSendResult>;
  review: (texts: string[], message: string, unsafe?: boolean) => void;
  emergencyStop: () => Promise<void>;
}) {
  const latest = useRef(options);
  latest.current = options;
  const startedContext = useRef("");
  const startedEstop = useRef(false);
  // A later release ends the exception granted by explicitly starting while already stopped.
  if (!options.estop) startedEstop.current = false;
  const [state, setState] = useState<VoiceSnapshot>({ status: "off", sessionId: null, queued: 0, message: "", duplex: false, level: 0, preview: "" });
  const controller = useRef<ContinuousVoice | null>(null);
  if (!controller.current) controller.current = new ContinuousVoice({
    bridge: () => window.CoyoteVoice,
    createSessionId: () => crypto.randomUUID(),
    allowed: () => latest.current.enabled && latest.current.active
      && latest.current.contextKey === startedContext.current
      && (!latest.current.estop || startedEstop.current),
    blocked: () => latest.current.blocked(),
    captureBlocked: () => latest.current.captureBlocked?.() ?? false,
    submit: (sentence) => latest.current.submit(sentence),
    review: (texts, message, unsafe) => latest.current.review(texts, message, unsafe),
    emergencyStop: () => latest.current.emergencyStop(),
    changed: setState,
  });
  const voice = controller.current;
  useLayoutEffect(() => {
    if (!options.enabled || !options.active || options.contextKey !== startedContext.current || (options.estop && !startedEstop.current)) voice.stop();
  }, [voice, options.enabled, options.active, options.contextKey, options.estop]);
  useEffect(() => { voice.notifyReady(); }, [voice, options.busy]);
  useEffect(() => {
    const stop = () => voice.stop();
    const estop = (event: Event) => { if ((event.target as Element | null)?.closest?.("[data-coyote-estop]")) stop(); };
    // Switching apps is allowed. Native's foreground service owns screen-lock cancellation
    // and sends off for this session; a true page unload still stops immediately.
    document.addEventListener("click", estop, true);
    window.addEventListener("pagehide", stop);
    window.addEventListener("coyote:voice-stop", stop);
    return () => {
      document.removeEventListener("click", estop, true);
      window.removeEventListener("pagehide", stop);
      window.removeEventListener("coyote:voice-stop", stop);
      voice.stop();
    };
  }, [voice]);
  return {
    ...state,
    stop: () => voice.stop(),
    clearFeedback: () => voice.clearFeedback(),
    pauseForError: (message: string) => voice.pauseForError(message),
    start: () => {
      startedContext.current = latest.current.contextKey;
      startedEstop.current = latest.current.estop;
      voice.start();
    },
  };
}
