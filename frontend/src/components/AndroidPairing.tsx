import { useApp } from "../store";
import PairingActions from "./PairingActions";

export default function AndroidPairing() {
  const controllerId = useApp((store) => store.state?.relay?.controller_id);
  return <div className="android-pair-inline">
    <p className="android-small">通过 DG-LAB4 的「Socket V4」连接设备。可扫码，或复制链接后打开官方 App。</p>
    <PairingActions controllerId={controllerId} />
    {controllerId && <a className="android-button" href={`/api/qrcode.png?controller_id=${encodeURIComponent(controllerId)}`} download="dglab-pair.png">保存二维码</a>}
    <p className="android-small">配对不会解除急停；未连接时仍可文字聊天。</p>
  </div>;
}