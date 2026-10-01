import { useEffect, useState } from "react";
import { useApp } from "../store";
import AndroidDialog from "./AndroidDialog";
import PairingActions from "./PairingActions";

export default function AndroidPairingDialog({ onClose }: { onClose: () => void }) {
  const controllerId = useApp((store) => store.state?.relay?.controller_id);
  const paired = useApp((store) => store.state?.relay?.status === "paired");
  const [qrError, setQrError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => { setQrError(false); setRetry(0); }, [controllerId]);
  useEffect(() => { if (paired) onClose(); }, [paired, onClose]);
  useEffect(() => {
    if (!qrError || retry >= 3) return;
    const timer = window.setTimeout(() => { setQrError(false); setRetry((value) => value + 1); }, 3000);
    return () => window.clearTimeout(timer);
  }, [qrError, retry]);
  return <AndroidDialog title="连接 DG-LAB4" id="android-pair-title" onClose={onClose}>
    <p className="android-small">在 DG-LAB4 的「Socket V4」中扫码，或复制配对链接。</p>
    <div className="android-pair-qr">
      {controllerId && !qrError ? <img key={`${controllerId}:${retry}`} alt="DG-LAB4 配对二维码" width={208} height={208}
        src={`/api/qrcode.png?controller_id=${encodeURIComponent(controllerId)}&retry=${retry}`} onError={() => setQrError(true)} />
        : <div role="status">{qrError ? "二维码加载失败" : "正在连接中继…"}{qrError && <button type="button" className="android-button" onClick={() => { setRetry(0); setQrError(false); }}>重试</button>}</div>}
    </div>
    <PairingActions controllerId={controllerId} />
    <p className="android-small android-pair-protection">配对后保持急停保护，可稍后在设备页连接。</p>
    <button type="button" className="android-button android-dialog-cancel" onClick={onClose}>取消</button>
  </AndroidDialog>;
}
