import { ChevronRight, Github, MessageSquare } from "lucide-react";

export const PUBLIC_REPOSITORY_URL = "https://github.com/romhuj/coko-dg";

export default function AndroidAbout() {
  return <section className="android-about" aria-label="关于 coko DG">
    <a href="https://x.com/_Good_Dick_" target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label="反馈（外部浏览器）"><MessageSquare size={21} /><span>反馈</span><ChevronRight size={19} /></a>
    <a href={PUBLIC_REPOSITORY_URL} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label="源仓库（外部浏览器）"><Github size={21} /><span>源仓库</span><ChevronRight size={19} /></a>
  </section>;
}
