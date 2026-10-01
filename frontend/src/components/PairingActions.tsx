import { useEffect, useRef, useState } from "react";
import { Copy, ExternalLink } from "lucide-react";
import { api } from "../api";
import { copyPairingLink } from "../pairingBridge";
import { useApp } from "../store";

export default function PairingActions({ controllerId }: { controllerId: string | null | undefined }) {
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const pending = useRef(false);
  const generation = useRef(0);
  const currentId = useRef(controllerId); currentId.current = controllerId;
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++; }; }, []);
  useEffect(() => { generation.current++; pending.current = false; setBusy(false); setUrl(""); setStatus(""); }, [controllerId]);
  const copy = async (open: boolean) => {
    if (pending.current || !controllerId || (open && !window.CoyotePairing)) return;
    window.dispatchEvent(new Event("coyote:voice-stop"));
    pending.current = true; setBusy(true); setStatus("");
    const token = generation.current;
    const valid = () => alive.current && token === generation.current && currentId.current === controllerId
      && useApp.getState().state?.relay?.controller_id === controllerId;
    try {
      const result = await api.pairUrl();
      if (!valid()) return;
      setUrl(result.url);
      const outcome = await copyPairingLink(result.url, open);
      if (valid()) setStatus(outcome.message || (outcome.copied ? "配对链接已复制" : "复制失败，请重试"));
    } catch (failure) { if (valid()) setStatus(failure instanceof Error ? failure.message : "无法获取配对链接"); }
    finally { if (alive.current && token === generation.current) { pending.current = false; setBusy(false); } }
  };
  return <div className="android-pair-actions">
    <button type="button" className="android-button" onClick={() => void copy(false)} disabled={!controllerId || busy}><Copy size={17} aria-hidden="true" />复制链接</button>
    <button type="button" className="android-button primary" onClick={() => void copy(true)} disabled={!controllerId || busy || !window.CoyotePairing}><ExternalLink size={17} aria-hidden="true" />复制并打开 DG-LAB4</button>
    {!window.CoyotePairing && <p className="android-small">打开 DG-LAB4 仅在 Android 应用中可用。</p>}
    {status && <p role="status" className="android-small">{status}</p>}
    {url && <textarea className="android-pair-link" aria-label="配对链接" readOnly value={url} rows={2} onFocus={(event) => event.target.select()} />}
  </div>;
}
