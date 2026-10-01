export interface PairingResult { type: "result"; requestId: string; status: "copied" | "opened" | "notInstalled" | "error"; copied: boolean; opened?: boolean; message?: string }
export interface PairingBridge { postMessage: (message: string) => void; onmessage: ((event: { data: string }) => void) | null }
declare global { interface Window { CoyotePairing?: PairingBridge } }

export async function copyPairingLink(pairUrl: string, open: boolean): Promise<PairingResult> {
  const bridge = window.CoyotePairing;
  const requestId = crypto.randomUUID();
  if (bridge) return new Promise((resolve, reject) => {
    const previous = bridge.onmessage;
    const finish = () => { clearTimeout(timer); if (bridge.onmessage === receive) bridge.onmessage = previous; };
    const receive = (event: { data: string }) => {
      let value: PairingResult;
      try { value = JSON.parse(event.data); } catch { return; }
      if (value?.type !== "result" || value.requestId !== requestId) { previous?.(event); return; }
      if (!["copied", "opened", "notInstalled", "error"].includes(value.status) || typeof value.copied !== "boolean") return;
      finish(); resolve(value);
    };
    const timer = setTimeout(() => { finish(); reject(new Error("未收到复制结果，请重试")); }, 10000);
    bridge.onmessage = receive;
    try { bridge.postMessage(JSON.stringify({ type: open ? "copyAndOpen" : "copy", requestId, pairUrl })); }
    catch (error) { finish(); reject(error); }
  });
  let copied = false;
  try { await navigator.clipboard.writeText(pairUrl); copied = true; }
  catch {
    const input = document.createElement("textarea"); input.value = pairUrl;
    input.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
    document.body.appendChild(input); input.select();
    try { copied = document.execCommand("copy"); } finally { input.remove(); }
  }
  return { type: "result", requestId, status: copied ? "copied" : "error", copied,
    message: copied ? (open ? "链接已复制，请在手机上打开 DG-LAB4" : "配对链接已复制") : "请长按下方链接复制" };
}
