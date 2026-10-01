import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../store";
import AndroidDialog from "./AndroidDialog";

export default function AndroidModelJudgment({ disabled, onDialogChange }: { disabled: boolean; onDialogChange: (open: boolean) => void }) {
  const enabled = useApp((store) => store.state?.model_judgment ?? false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  useEffect(() => { onDialogChange(open); return () => onDialogChange(false); }, [open, onDialogChange]);
  const apply = (value: boolean) => {
    const state = useApp.getState().state;
    if (state) useApp.getState().setState({ ...state, model_judgment: value });
  };
  const toggle = async () => {
    if (pending.current || (disabled && !enabled)) return;
    window.dispatchEvent(new Event("coyote:voice-stop"));
    setError("");
    if (!enabled) { setOpen(true); return; }
    pending.current = true; setBusy(true);
    try { await api.setModelJudgment(false); apply(false); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "关闭失败，请重试"); }
    finally { pending.current = false; setBusy(false); }
  };
  return <div className="android-model-judgment">
    <div className="android-setting-row"><span>模型自判断</span><button type="button" role="switch" aria-label="模型自判断" aria-checked={enabled} disabled={busy || (disabled && !enabled)} className={`android-judgment-switch ${enabled ? "active" : ""}`} onClick={() => void toggle()}><span aria-hidden="true" /></button></div>
    {error && <p role="alert" className="android-compose-error">{error}</p>}
    {open && <JudgmentConfirmation onClose={() => setOpen(false)} onEnabled={() => { apply(true); setOpen(false); }} />}
  </div>;
}

function JudgmentConfirmation({ onClose, onEnabled }: { onClose: () => void; onEnabled: () => void }) {
  const [token, setToken] = useState("");
  const [remaining, setRemaining] = useState(5);
  const [preparing, setPreparing] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const deadline = useRef(Number.POSITIVE_INFINITY);
  const pending = useRef(false);
  useEffect(() => {
    let alive = true;
    setPreparing(true); setToken(""); setRemaining(5); setError("");
    api.prepareModelJudgment().then((result) => {
      if (!alive) return;
      if (!result.confirmation_token || !Number.isFinite(result.wait_seconds)) throw new Error("确认信息无效，请重试");
      const wait = Math.max(5, result.wait_seconds);
      deadline.current = performance.now() + wait * 1000;
      setRemaining(Math.ceil(wait)); setToken(result.confirmation_token);
    }).catch((failure) => { if (alive) setError(failure instanceof Error ? failure.message : "无法准备确认，请重试"); })
      .finally(() => { if (alive) setPreparing(false); });
    const timer = window.setInterval(() => { if (alive && Number.isFinite(deadline.current)) setRemaining(Math.max(0, Math.ceil((deadline.current - performance.now()) / 1000))); }, 200);
    return () => { alive = false; window.clearInterval(timer); };
  }, [attempt]);
  const confirm = async () => {
    if (pending.current || !token || performance.now() < deadline.current || remaining > 0) return;
    pending.current = true; setBusy(true); setError("");
    try { await api.setModelJudgment(true, token); onEnabled(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "开启失败，请重试"); setToken(""); }
    finally { pending.current = false; setBusy(false); }
  };
  return <AndroidDialog title="开启模型自判断？" id="model-judgment-title" busy={busy} onClose={onClose}>
    <div className="android-judgment-warning">
      <p>开启后，角色会自行判断情景文字，决定维持、增加、减少或停止输出。</p>
      <p>普通拒绝措辞不一定等同急停。需要立即停止，请点击急停或明确说「急停／停止设备」。</p>
      <p>息屏停止及 A/B 通道上限始终生效。</p>
    </div>
    {error && <p role="alert" className="android-compose-error">{error}</p>}
    {error && !token && <button type="button" className="android-button" disabled={busy || preparing} onClick={() => { deadline.current = Number.POSITIVE_INFINITY; setAttempt((value) => value + 1); }}>重新准备</button>}
    <div className="android-dialog-actions"><button type="button" className="android-button" disabled={busy} onClick={onClose}>取消</button>
      <button type="button" className="android-button primary" disabled={busy || preparing || !token || remaining > 0} onClick={() => void confirm()}>{busy ? "保存中…" : preparing ? "准备中…" : remaining > 0 ? `确认开启（${remaining}秒）` : "确认开启"}</button></div>
  </AndroidDialog>;
}
