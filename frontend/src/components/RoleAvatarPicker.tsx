import { useEffect, useRef, useState } from "react";
import { ImagePlus, RotateCcw } from "lucide-react";
import { prepareRoleAvatar } from "../roleAvatar";

export default function RoleAvatarPicker({ id, name, value, disabled, onChange, onBusyChange }: {
  id: string; name: string; value?: string | null; disabled: boolean;
  onChange: (value: string | null) => void; onBusyChange: (busy: boolean) => void;
}) {
  const input = useRef<HTMLInputElement>(null), generation = useRef(0);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => () => { generation.current++; }, []);
  const choose = async (file?: File) => {
    if (!file || disabled || busy) return;
    const token = ++generation.current;
    setBusy(true); onBusyChange(true); setError("");
    try { const avatar = await prepareRoleAvatar(file); if (token === generation.current) onChange(avatar); }
    catch (failure) { if (token === generation.current) setError(failure instanceof Error ? failure.message : "图片处理失败，请重新选择。"); }
    finally { if (token === generation.current) { setBusy(false); onBusyChange(false); } }
  };
  return <div className="android-avatar-picker">
    <div className="android-avatar-preview" aria-label="头像预览">{value ? <img src={value} alt="所选角色头像" /> : <span aria-hidden="true">{[...name.trim()][0] || "角"}</span>}</div>
    <div className="android-avatar-controls">
      <input id={id} ref={input} type="file" accept="image/*" hidden disabled={disabled || busy} onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; void choose(file); }} />
      <button type="button" className="android-button" disabled={disabled || busy} onClick={() => input.current?.click()}><ImagePlus size={18} />{busy ? "处理图片…" : value ? "更换头像" : "选择头像"}</button>
      {value && <button type="button" className="android-avatar-reset" disabled={disabled || busy} onClick={() => { setError(""); onChange(null); }}><RotateCcw size={14} />恢复默认头像</button>}
    </div>
    {error && <p className="android-compose-error" role="alert">{error}</p>}
  </div>;
}
