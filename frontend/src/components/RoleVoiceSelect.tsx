import { SPEECH_VOICES, type SpeechVoiceId } from "../replySpeech";

export default function RoleVoiceSelect({ id, value, disabled, onChange, includeSystem = false }: {
  id: string; value: SpeechVoiceId; disabled: boolean; onChange: (voiceId: SpeechVoiceId) => void; includeSystem?: boolean;
}) {
  return <div>
    <label className="android-field-label" htmlFor={id}>音色</label>
    <select id={id} className="android-field" value={value} disabled={disabled}
      aria-describedby={`${id}-hint`} onChange={(event) => {
        const voice = SPEECH_VOICES.find((item) => item.id === event.target.value);
        if (voice) onChange(voice.id);
      }}>
      {SPEECH_VOICES.filter((voice) => includeSystem || voice.provider === "offline").map((voice) => <option key={voice.id} value={voice.id}>{voice.label}</option>)}
    </select>
    <p id={`${id}-hint`} className="android-small mt-2">首次使用下载音色</p>
  </div>;
}
