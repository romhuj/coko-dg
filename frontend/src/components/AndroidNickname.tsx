import { useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../store";
import AndroidDialog from "./AndroidDialog";

export default function AndroidNickname({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState(useApp.getState().state?.config_info.player_nick ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  const save = async () => {
    if (pending.current) return;
    const nick = name.trim();
    if (!nick || [...nick].length > 20) { setError("请输入 1–20 字的名称"); return; }
    pending.current = true; setBusy(true); setError("");
    try {
      await api.setNick(nick);
      const state = useApp.getState().state;
      if (state) useApp.getState().setState({ ...state, config_info: { ...state.config_info, player_nick: nick } });
      onClose();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "保存失败，请重试"); }
    finally { pending.current = false; setBusy(false); }
  };
  return <AndroidDialog title="我的名称" id="android-nickname-title" busy={busy} onClose={onClose}>
    <p className="android-small">角色会用这个名称称呼你。</p>
    <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label className="android-field-label" htmlFor="android-player-name">名称</label>
      <input id="android-player-name" className="android-field" value={name} autoComplete="nickname" disabled={busy} onChange={(event) => setName(event.target.value)} />
      {error && <p className="android-compose-error" role="alert">{error}</p>}
      <div className="android-dialog-actions"><button type="button" className="android-button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="android-button primary" disabled={busy}>{busy ? "保存中…" : "保存"}</button></div>
    </form>
  </AndroidDialog>;
}
