"""Small, conservative local stop policy; never derive stimulation from prose."""
import re
import unicodedata


def stop_intent(text: str, *, model_judgment: bool = False) -> str | None:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    normalized = re.sub(r"[。！？!?.,，；;、:：\s]+$", "", normalized)
    if normalized in {"急停", "停止设备", "stop", "estop"}:
        return "emergency"
    if model_judgment:
        return None
    # Whole clauses only. Quotes, hypotheticals, negation and descriptions such
    # as '不要停止'/'她说停止' must not be converted into an emergency command.
    clauses = re.split(r"[。！？!?，,；;\n]+", normalized)
    for clause in clauses:
        clause = clause.strip()
        if re.fullmatch(r"(?:请|请你|麻烦|麻烦你|马上|立刻|现在|先|快|赶紧|给我)*"
                        r"(?:停止|停下|停下来|停一下|暂停|别继续|不要继续|不要再继续)(?:吧|啊|了|一下)?", clause):
            return "stop"
        if re.fullmatch(r"(?:我|现在我|我现在)?(?:真的|已经|有点|很|太)?"
                        r"(?:不舒服|难受|受不了|疼|痛|疼痛|无法忍受|承受不了|撑不住|不想继续)"
                        r"(?:了|啊|啦|，请停下)?", clause):
            return "discomfort"
    return None
