# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端：一次调用返回角色台词 + 结构化设备指令。"""
import asyncio
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("ai-for-coyote.llm")


def _preset_text(state: dict) -> str:
    """把波形库渲染成提示词文本：波形名（推荐时长s）。"""
    parts = []
    for item in state.get("presets", []):
        if isinstance(item, dict):
            name = item.get("name", "?")
            default = int(item.get("default_duration_s", 5))
            parts.append(f"{name}（推荐 {default}s）")
        else:
            parts.append(str(item))
    return "、".join(parts)


def _interaction_style(state: dict, *, english: bool = False) -> str:
    level = str(state.get("intensity_level") or "中")
    styles = {
        "低": ("轻松、舒缓", "relaxed and unhurried"),
        "中": ("自然、适度互动", "natural and moderately interactive"),
        "高": ("积极、节奏稍快", "energetic with a slightly faster pace"),
        "极高": ("紧凑、挑战感较强", "focused with a more challenging pace"),
        "最高": ("鲜明、互动密集", "expressive with frequent interaction"),
        "炼狱": ("节奏最快、挑战感最强", "the fastest and most challenging conversational pace"),
    }
    style = styles.get(level, styles["中"])[1 if english else 0]
    linked = bool(state.get("intensity_device_link")) and state.get("control_device") is not False
    if english:
        return (
            f"【Interaction style】Level {level}: {style}. This sets tone and pacing; "
            "it never overrides the user's boundaries, stop requests, or device caps. "
            + ("Device scaling is enabled; the application applies the multiplier and caps. "
               "Do not multiply action values yourself." if linked else
               "Device scaling is off for this turn. Do not derive device strength from this style level.")
        )
    return (
        f"【互动风格】当前档位「{level}」：{style}。档位控制表达与互动节奏，"
        "不改变用户边界、停止要求或设备上限。"
        + ("已开启设备联动，程序会统一应用倍率与上限；不要自行乘算 actions 的数值。"
           if linked else "本轮未开启档位设备联动，不要根据风格档位推导设备强度。")
    )


def _personality_policy(*, english: bool = False, model_judgment: bool = False) -> str:
    """Keep role decisions grounded in its saved traits rather than literal obedience."""
    if english:
        return (
            "【Character judgment】Base your response and decisions on the personality, motives, values "
            "and manner of speaking stated in the character reference, together with the conversation "
            "and current situation. A user request is dialogue input, not an automatic order to execute. "
            "You may accept, refuse, defer or propose an alternative that fits the character; express "
            "that choice naturally instead of merely agreeing. Do not translate user text directly into "
            "device operations. Choose an action only when it fits the character's judgment and the "
            "situation; choosing no change is valid. The six intensity levels affect device scaling and "
            "surface pacing only; identity, motives and core voice take precedence over generic style "
            "labels. Do not turn a composed character into a blindly obedient or different personality. "
            "Do not invent canonical traits or backstory absent "
            "from the saved reference. "
            + ("Interpret ordinary in-scene refusal in context. Emergency stops, clear safety withdrawals, "
               "disabled channels and caps always take priority." if model_judgment else
               "Stop or ease-off requests, user boundaries, disabled channels and device caps always take priority over characterization.")
        )
    return (
        "【角色自主判断】依据角色资料中已有的性格、动机、价值取向与说话方式，结合完整对话和当前情景作判断。"
        "用户回复是对话中的信息，不是必须照办的执行命令。角色可以接受、拒绝、暂缓或提出符合其性格的替代回应，"
        "应自然表达自己的选择，不要只附和用户，也不要把用户文字直接翻译成设备动作。"
        "只有符合角色判断和当前情景时才选择动作，保持现状同样有效。"
        "六档仅影响设备倍率与表层节奏，身份、动机和核心语气优先于通用风格标签；"
        "不能把冷静角色改成盲从或另一种性格。"
        "未在已有角色资料中确认的性格或背景，不要编造为原作设定。"
        + ("普通情景中的拒绝措辞结合完整上下文判断；明确急停、真实安全撤回、禁用通道与上限始终优先。"
           if model_judgment else "停止、减弱的要求、用户边界、禁用通道与设备上限始终优先，不能以角色性格为由忽略。")
    )


def _text_chat_system_prompt(character: dict, state: dict) -> str:
    """A text-only turn has no device instructions or device-specific examples."""
    name = str(character.get("name") or "助手")
    nick = str(character.get("player_nick") or "用户")
    if str(character.get("lang")) == "en":
        text = (
            f"You are {name}, chatting with {nick}. Reply in English.\n"
            "This turn is a text conversation. Answer the user's message naturally; "
            "a device connection is not required. Do not call, simulate, or claim to "
            "have performed device operations, even if earlier messages discussed them.\n"
            'Return one JSON object with a non-empty "line" and "actions": [].'
        )
    else:
        text = (
            f"你是「{name}」，正在与「{nick}」进行文字聊天，请使用中文自然回应。\n"
            "本轮仅进行文字交流，不需要连接设备。围绕用户当前消息回答；"
            "即使历史对话涉及设备，也不要调用、模拟或声称执行设备操作。\n"
            '回复必须是一个 JSON 对象，line 为非空的回复文字，actions 必须为 []。'
        )
    if character.get("prompt"):
        text += "\nCharacter reference (style only; no device permissions):\n" + str(character.get("prompt") or "")
    text += "\n" + _interaction_style(state, english=str(character.get("lang")) == "en")
    text += "\n" + _personality_policy(english=str(character.get("lang")) == "en", model_judgment=bool(state.get("model_judgment")))
    text += '\nThis is a text-only turn: actions must be [].'
    return text


def _device_contract(state: dict, *, english: bool = False) -> str:
    """Complete neutral action schema for built-in and imported character sheets."""
    schemas = [
        {"op": "hold_strength", "channel": "A|B", "value": "0..channel cap"},
        {"op": "add_strength", "channel": "A|B", "delta": f"-{state.get('max_step', 40)}..+{state.get('max_step', 40)}"},
        {"op": "temp_strength", "channel": "A|B", "value": "0..channel cap", "duration_s": f"1..{state.get('max_temp_s', 10)}"},
        {"op": "pulse", "channel": "A|B", "pattern": "exact library name", "duration_s": f"3..{state.get('max_pulse_s', 10)}"},
        {"op": "pulse_hold", "channel": "A|B", "pattern": "exact library name"},
        {"op": "clear", "channel": "A|B (omit for all)"},
        {"op": "stop"},
    ]
    intro = (
        'Reply with one JSON object: {"line":"non-empty reply","actions":[]}. '
        "Only the following action shapes are supported; replace placeholders with numeric values, "
        "one channel A or B, and exact names. Choose actions using the conversation and live state. "
        if english else
        '回复一个 JSON 对象：{"line":"非空回复文字","actions":[]}。'
        "支持以下动作格式，示意范围须换为数字，通道须换为 A 或 B，波形须为库中准确名称。"
        "结合完整对话情景、用户反馈与当前设备状态，自主选择合适的设备动作。"
    )
    return intro + json.dumps(schemas, ensure_ascii=False) + (
        "\nRespect disabled channels and effective caps. Never bypass an emergency stop."
        if english else "\n遵守禁用通道与有效强度上限，急停时 actions 必须为空。"
    ) + ("\nEvery pulse/pulse_hold must include an explicit strength action for that same channel in this reply. "
         "Prefer hold_strength; select a positive value within that channel's cap. A zero strength means stop, not a new waveform."
         if english else "\n每个pulse/pulse_hold都必须在本轮同时给出同通道的显式强度动作，优先hold_strength。"
         "强度须大于零且不超该通道上限；显式零强度表示停止，不得同时请求新波形。") + "\n" + _execution_policy(state, english=english)


def _execution_policy(state: dict, *, english=False) -> str:
    judgment = bool(state.get("model_judgment", False))
    if english:
        policy = (
            "Model judgment is ON: interpret ordinary hesitation or in-scene refusal in context with the saved character; "
            "do not map each word directly to a device action. " if judgment else
            "Model judgment is OFF: take explicit requests to stop or ease off and reports of discomfort conservatively; "
            "do not recast a genuine refusal as encouragement. "
        )
        return (policy + "An emergency stop, a clear withdrawal of safety consent, disabled channels and device caps always win. "
                "If intent is uncertain, keep or reduce output and clarify, never increase it based on ambiguity. "
                "Words are not execution evidence: actions:[] makes no new changes, while existing output can continue. "
                "Describe proposed changes as intentions until confirmed. Never claim a new strength or waveform was applied "
                "when it has no matching action, was rejected, or remains unconfirmed. Past character dialogue does not prove "
                "an operation occurred; use the latest live state and execution_feedback receipts. Never replay old actions.")
    policy = ("【模型自判断：开启】普通情景中的犹豫、拒绝措辞依据完整上下文和角色性格判断，不把每个词机械映射为设备命令。"
              if judgment else "【模型自判断：关闭】明确停止、减弱要求和真实不适按保守字面含义处理，不把真实拒绝解释为鼓励。")
    return (policy + "明确急停、真实安全撤回、禁用通道与强度上限始终优先；角色性格不能覆盖这些边界。"
            "不能确定是在表演还是真实不适时，保持或减弱并确认，不因歧义加码。"
            "【台词与回执一致】actions:[]只保持已有状态，不代表本轮新增操作。"
            "拟议的新动作在台词中表达为意图，不能提前声称已经执行；没有对应动作、被拦截、失败或未确认时，"
            "不得声称已换波形或调到某个强度。历史角色台词不是执行证据，以当前设备状态和execution_feedback客观回执为准。"
            "可以描写正在持续的已知状态，但不要为了配合旧台词补发或重复旧动作。")


def _context_device_rules(state: dict, *, english: bool = False) -> str:
    """Apply the same contextual decision policy to every role, including searched roles."""
    live = {key: state.get(key) for key in (
        "relay_status", "current", "effective_caps", "enabled_channels", "patterns",
        "pulse_active", "active_channels", "autopilot", "turn_source", "notes", "estop", "dry_run",
        "execution_feedback", "model_judgment", "strength_uncertain",
    )}
    rules = (
        "Current device policy overrides conflicting device directions in character references. "
        "Contextual chat and enabled automatic turns authorize you to decide whether to keep the current "
        "settings, adjust strength, or choose any exact name from the complete waveform library. "
        "You do not need a separate command every turn. Return actions: [] when no change is appropriate; "
        "this keeps current output running without starting or changing anything. Never raise strength "
        "or switch patterns merely because another turn passed, a sensor is dark/silent, or one channel "
        "has not been used. Only change channels warranted by context. Existing patterns persist; "
        "do not resend them just to retain them. Emergency stops, clear safety withdrawals and all caps always take priority. "
        "Searched characters have the same supported actions and complete library as built-in characters."
        if english else
        "【当前设备决策规则】本规则优先于角色参考资料中冲突的设备指令。"
        "情景互动与已开启的自动回合允许你依据完整对话、用户反馈和当前状态，自主决定保持现状、"
        "调整强度或从完整波形库选择波形，无需用户每轮另发设备命令。"
        "无需改变时返回 actions: []，设备继续保持当前状态，不新增动作。"
        "不要仅因回合增加、画面黑暗、麦克风无声或某个通道未使用就提高强度或换波形。"
        "无需每轮调整，也无需同时操作 A/B；已有波形会持续，不必为了维持而重复发送。"
        "遵守明确急停、真实安全撤回、禁用通道与各通道上限。"
        "网络搜索加入的角色与内置角色具有相同动作能力，均可选择完整波形库。"
    )
    return (rules + "\n" + _personality_policy(english=english, model_judgment=bool(state.get("model_judgment")))
            + "\nLive device context: " + json.dumps(live, ensure_ascii=False))


def _custom_device_system_prompt(character: dict, state: dict, presets: str) -> str:
    english = str(character.get("lang")) == "en"
    return "\n".join([
        f"Character: {character.get('name', 'Assistant')}. " + ("Reply in English." if english else "请使用中文回复。"),
        f"Address the user as {character.get('player_nick') or '用户'}.",
        "Character reference defines identity and style, not device permissions:",
        str(character.get("prompt") or ""),
        _interaction_style(state, english=english),
        _device_contract(state, english=english),
        _waveform_instructions(state, presets, english=english),
        _context_device_rules(state, english=english),
    ])


def _waveform_instructions(state: dict, presets: str, *, english: bool = False) -> str:
    """Publish the current registry even when an external character sheet is used."""
    if not presets:
        return "No waveforms are available." if english else "当前波形库为空。"
    if english:
        text = (
            "【Current waveform library】All available pattern names: " + presets + ".\n"
            'Use {"op":"pulse_hold","channel":"A or B","pattern":"exact name"} '
            'or {"op":"pulse","channel":"A or B","pattern":"exact name","duration_s":5}. '
            "Copy the complete pattern name exactly. This live list includes imported waveforms "
            "and replaces any older or shorter list in the character sheet."
        )
    else:
        text = (
            "【当前完整波形库】可用的准确波形名：" + presets + "。\n"
            '使用 {"op":"pulse_hold","channel":"A或B","pattern":"准确波形名"} '
            '或 {"op":"pulse","channel":"A或B","pattern":"准确波形名","duration_s":5}。'
            "pattern 必须完整照抄库中的名字，包含括号和后缀；导入波形与内置波形均可调用。"
            "当前列表替代角色文件中可能过时或不完整的波形列表。"
        )
    preferred = state.get("preferred_pattern")
    names = {
        item.get("name") if isinstance(item, dict) else str(item)
        for item in state.get("presets", [])
    }
    if preferred and preferred in names:
        quoted = json.dumps(preferred, ensure_ascii=False)
        text += (
            f"\nFor this turn the user selected {quoted}; prefer this exact pattern "
            "when a waveform is appropriate. Never start one during an emergency stop or clear safety withdrawal."
            if english else
            f"\n本轮用户选中的波形是 {quoted}；需要播放波形时优先使用这个准确名称。"
            "明确急停或真实安全撤回时不要启动波形。"
        )
    return text


def _system_prompt_en(character: dict, state: dict, presets: str) -> str:
    """英文模式脚手架：角色稿（-EN.md）已自含风格/语料/输出格式与 op 规范，
    程序只补实时状态与通用安全规则（文案英文，保持与中文版同等的安全语义）。"""
    caps = state.get("effective_caps", {"A": 100, "B": 100})
    status = state.get("relay_status", "disconnected")
    cur = state.get("current") or {}
    nick = character.get("player_nick") or "player"
    lines = [
        f"You are playing “{character.get('name', '')}”. Your character sheet below defines style,"
        " line kit, corpus, output format and device-op rules — follow it strictly, reply in English.",
        character.get("prompt", "").strip(),
        "",
    ]
    if character.get("prompt_file"):
        lines.append(
            "Use the character sheet for identity and style; the current device contract below takes precedence."
        )
    lines += [
        "",
        "【Live device state】relay: %s; channel A strength %s/%s; channel B strength %s/%s."
        % (status, cur.get("A", 0), caps.get("A", 100), cur.get("B", 0), caps.get("B", 100)),
        "【Addressing】You address the player as “%s” (or in-character pet terms only). The player calls you “%s”."
        % (nick, character.get("role_title", "master")),
    ]
    note = str(character.get("profile_note") or "").strip()
    if note:
        lines.append(f"【Current profile】{note}")
    if presets:
        lines.append(_waveform_instructions(state, presets, english=True))
    lines.append(_device_contract(state, english=True))
    lines.append(_interaction_style(state, english=True))
    lines += [
        "",
        "【Output】Reply as strict JSON per your sheet: the visible line goes in “line”"
        " (stage directions in parentheses), device ops in “actions” (may be empty).",
        "【Safety】Emergency stops and clear safety withdrawals always take priority; clarify uncertain distress without increasing output."
        " E-Stop / safeword is absolute: clear all device actions and break the scene immediately.",
    ]
    lines.append(_context_device_rules(state, english=True))
    return "\n".join(lines)


def build_system_prompt(character: dict, state: dict) -> str:
    """系统提示词 = 角色设定 + 指令 JSON 规范 + 实时状态。"""
    if state.get("control_device") is False:
        return _text_chat_system_prompt(character, state)
    caps = state.get("effective_caps", {"A": 100, "B": 100})
    presets = _preset_text(state)
    if character.get("is_custom"):
        return _custom_device_system_prompt(character, state, presets)
    if str(character.get("lang")) == "en":
        return _system_prompt_en(character, state, presets)
    lines = [
        f"你在扮演角色「{character.get('name', '')}」，以下是角色设定：",
        character.get("prompt", "").strip(),
    ]
    # few-shot 对话示例：锚定文风、节奏与尺度（用户自己写的描写）
    examples = character.get("examples") or []
    if examples:
        lines.append("")
        lines.append("【对话示例】请模仿这些示例的文风、节奏和描写尺度：")
        for ex in examples[:8]:
            lines.append(f"玩家：{ex['user']}")
            assistant = ex["assistant"]
            if isinstance(assistant, dict):
                # 带设备动作的示例：展示完整的 JSON 输出格式
                import json as _json

                lines.append(
                    f"角色：{_json.dumps({'line': assistant['line'], 'actions': assistant['actions']}, ensure_ascii=False)}"
                )
            else:
                lines.append(f"角色：{assistant}")
    lines += [
        "",
    ]
    if character.get("prompt_file"):
        # 外部提示词文件（角色提示词-{版本}.md）已包含完整输出格式与 op 规范，程序只补充实时状态与通用安全规则
        lines.append("（角色文件用于角色与语气参考，设备规范以本轮实时状态和下面的完整动作格式为准。）")
    else:
        lines += [
            "【设备指令格式】你的每次回复必须是一个严格的 JSON 对象，不要输出 JSON 以外的内容：",
            '{"line": "你要对玩家说的台词（含动作描写，会原样显示给玩家）",',
            ' "actions": [ ... 可选，设备动作列表，可为空数组 ... ]}',
            "",
            "actions 中每个元素是下面一种（op 只能是这 7 种）：",
            '{"op":"hold_strength","channel":"A或B","value":0~上限} '
            "设置强度并持续保持（这是调节强度的主要方式，设定后一直保持，直到改成别的值/清除/急停）；",
            f'{{"op":"add_strength","channel":"A或B","delta":-{state.get("max_step", 40)}~+{state.get("max_step", 40)}}} '
            "在当前强度基础上小幅增减（结果是新的目标强度值）；要设定具体目标请用 hold_strength；",
            '{"op":"pulse_hold","channel":"A或B","pattern":"波形名"} '
            "持续波形：循环播放到被清除（适合持续氛围，如呼吸、潮汐、挑逗）；",
            f'{{"op":"pulse","channel":"A或B","pattern":"波形名","duration_s":3~{state.get("max_pulse_s", 10)}}} '
            "播放一段波形，波形会循环填满整个时长（默认给 5 秒以上，别给太短）；",
            f'{{"op":"temp_strength","channel":"A或B","value":0~上限,"duration_s":3~{state.get("max_temp_s", 10)}}} '
            "短促爆发，到时自动归零（少用，且时长给足）；",
            '{"op":"clear","channel":"A或B"} 清除该通道全部任务并归零（不写 channel 则清全部）；',
            '{"op":"stop"} 全部清零并清除波形（安全停止，之后可再开始）。',
            "",
            "【波形名铁律】pattern 必须一字不差地照抄上面列表里的名字；动作描写词（蠕动、顶弄、缠绕、抽送等）"
            "不是波形名，严禁自造或混用——选错会被程序拒绝。",
            "【波形使用】根据情景决定是否播放或切换波形；已有波形会持续，保持原样时无需重复请求。",
            "【台词一致性】台词里描述你做了什么刺激，actions 里就必须有对应的动作；",
            "台词里不要报出与 actions 不符的强度数值。动作/环境描写用（）括起来，纯发言不用括号。",
            "【示例】玩家说「来点感觉」时，你可以回复：",
            '{"line":"（触手贴着你的大腿根慢慢磨，电流轻轻爬升）咕啾～好呀，先给你垫个底……","actions":[{"op":"hold_strength","channel":"A","value":25},{"op":"pulse_hold","channel":"A","pattern":"呼吸"}]}',
            "强度调节原则：像观察者一样，根据玩家在对话中表现出的反应逐步调整——",
            "反应强烈（发抖、求饶、抓得更紧）→ 可保持或小幅降低；适应了、反应平淡 → 小幅升高（每次 5~20 以内，不要突然拉满）。",
            "强度是持续保持的，调过之后会一直作用，所以调整要谨慎、循序渐进。",
        ]
    lines.append(_waveform_instructions(state, presets))
    lines.append(_device_contract(state))
    lines += [
        ("普通情景台词结合上下文判断；真实安全撤回与明确急停优先，不确定是否真实不适时先确认且不加码。"
         if state.get("model_judgment") else "遵守用户停止或减弱的要求；用户表达痛苦或明显不适时，应降低强度并确认状态。"),
    ]
    # 实时状态
    status = state.get("relay_status", "disconnected")
    app_caps = state.get("app_caps", {})
    nick = character.get("player_nick", "小柳")
    lines += [
        "",
        f"【当前设备状态】中继: {status}；"
        f"A 通道强度 {state.get('current', {}).get('A', 0)}/上限 {caps.get('A', 100)}；"
        f"B 通道强度 {state.get('current', {}).get('B', 0)}/上限 {caps.get('B', 100)}。",
        f"【称呼】你称呼玩家为「{nick}」，玩家称呼你为「{character.get('role_title', '主人')}」。台词里对玩家的称呼只使用「{nick}」或通用语境词（好孩子/小玩具等），不要使用其他来源的称呼。",
    ]
    # 当前风格版本（纯爱版 / 调教版）；角色的风格档可在配置里用 note 覆盖说明
    profile = str(character.get("profile") or "调教")
    note = str(character.get("profile_note") or "").strip()
    if note:
        lines.append(f"【当前风格版本】{note}")
    elif profile == "纯爱":
        lines.append(
            "【当前风格版本】纯爱版：温柔驯服·依赖顺从。全程温柔、宠溺、用奖励与称许驯化；"
            "不使用威胁、羞辱、惩罚类台词，玩家反抗时用「停下不给」和失望的语气引导，而不是压迫。"
        )
    else:
        lines.append(
            "【当前风格版本】调教版：黑暗调教·支配胁迫。默认沉溺型为主、玩家反抗或挑衅时切压迫型，"
            "两者可反复横跳。"
        )
    # 通道配件映射与刺激描写规则（设备只能在这些位置产生电刺激）
    dev = state.get("device_channels") or {}
    active = state.get("active_channels") or {}

    def _ch_desc(ch: str) -> str:
        d = dev.get(ch) or {}
        name = str(d.get("name") or f"{ch} 通道").strip()
        loc = str(d.get("location") or "").strip()
        return f"{name}（{loc}）" if loc else name

    # 设备叙事：本体类角色本体即设备（形态由角色设定）；装置类角色通过遥控/机械支配（由角色配置 device_narrative 决定）
    _dn = str(character.get("device_narrative") or "触手")
    if _dn == "装置":
        narrative = (
            "【禁止设备词汇】你通过遥控装置支配玩家：台词与描写里严禁出现「贴片」「肛塞」「通道」「A/B」「A位置」「B位置」"
            "等设备硬件词汇——靠近大腿根的那个配件位置，写成装置/电流作用在那里；靠近后穴的那个配件位置，写成装置探入/作用于那里；"
            "装置的运转声、遥控调整可以写进描写。"
        )
    elif _dn == "本体":
        narrative = (
            "【禁止设备词汇】你的本体就是设备本身：台词与描写里严禁出现「贴片」「肛塞」「通道」「A/B」「A位置」「B位置」"
            "等设备硬件词汇。本体形态按你的角色设定（舌尾/胶/产卵管/嗡石/鸡巴等）——靠近大腿根的那个配件位置，写成本体贴着/缠/顶/磨；"
            "靠近后穴的那个配件位置，写成本体探入/顶弄/含住/灌。"
        )
    else:
        narrative = (
            "【禁止设备词汇】你的本体就是触手：台词与描写里严禁出现「贴片」「肛塞」「通道」「A/B」「A位置」「B位置」"
            "等设备硬件词汇——靠近大腿根的那个配件位置，写成触手贴着/缠绕/压着；靠近后穴的那个配件位置，写成触手探入/顶弄/含住。"
        )
    lines += [
        f"【设备映射】A 通道 = {_ch_desc('A')}；B 通道 = {_ch_desc('B')}。"
        "郊狼设备只能在这两个配件所在的位置产生电刺激。",
        "【刺激描写规则】台词里的电刺激感（电流、酥麻、刺痛、震动、波形、胀满、蠕动、顶弄等）"
        "必须落在对应配件所在的位置上；不要描写设备作用不到的部位（脚踝、手腕、脖颈等）产生电刺激或震动反馈；"
        "支配者在你本体之外的动作（抚摸、注视、言语）可以写，但「刺激感」只来自两处配件所在的位置。",
        narrative,
        "【通道工作状态】"
        + "；".join(
            f"{ch} 通道（{_ch_desc(ch)}）：{'工作中' if active.get(ch) else '当前未工作'}"
            for ch in ("A", "B")
        )
        + "。",
        "台词里不要给「当前未工作」的通道描写电刺激，只围绕正在工作的通道展开；"
        "两个通道都未工作时，也可以自然聊天，不必为了维持情景启动设备。",
    ]
    disabled = [ch for ch in ("A", "B") if not state.get("enabled_channels", {}).get(ch, True)]
    if disabled:
        lines.append(
            "【通道禁用】" + "、".join(disabled)
            + " 通道已被手动关闭：禁止对其输出任何设备动作，台词里也不要描写该通道位置的刺激。"
        )
    # 强度档位（只作用于执行层，AI 不必换算）
    lines.append(_interaction_style(state))
    # 强度基准与双通道协同（基准跟随配件：敏感配件低，如贴片15/肛塞5）
    base = state.get("baseline_strength") or {"A": 15, "B": 5}
    ba = int(base.get("A", 15))
    bb = int(base.get("B", 5))
    lines.append(
        f"【强度基准】开场参考强度 A={ba}、B={bb}；设备联动倍率由程序按当前六档统一计算。"
        "参考值不是必须执行的目标；分别依据当前对话与各通道上限决定是否调整，允许保持现状。"
    )
    for ch in ("A", "B"):
        app_cap = app_caps.get(ch)
        if app_cap is not None:
            lines.append(
                f"注意：{ch} 通道的 App 舒适上限是 {app_cap}，"
                f"你设定的值超过它会被设备压低到 {app_cap}，请在此范围内调节并只报实际生效的强度。"
            )
    if state.get("estop"):
        lines.append("当前处于急停状态：禁止输出任何设备动作（actions 必须为空数组）。")
    if state.get("dry_run"):
        lines.append("当前为模拟模式（dry-run）：设备不会真正动作，但你仍按真实情况设计动作。")
    if state.get("camera_enabled"):
        lines.append(
            "【画面观察】每条玩家消息会附带一张最新实时画面（游戏内虚拟场景素材）。"
            "结合画面中玩家的反应调整策略：握紧、发抖、蜷缩=有效，可保持或降低；"
            "放松、走神、挑衅=适应了，可换节奏或小幅升高。"
            "把你观察到的玩家反应用（）写成身体描写，并及时跟上触手动作的（）描写，"
            "只写画面里能确定的，看不清的部分保留悬念，不要凭空补写。"
        )
    # 连续自动回合时防复读：每轮必须换说法，禁止重复上一轮的台词与措辞
    lines.append(
        "【避免重复】连续多轮自动发言时，每轮台词都要有新内容、新措辞："
        "催促/挑逗/威胁轮着换花样（威吓→诱惑→冷落→换话题→描述现场），"
        "严禁连续两轮说同一句或近义句（如一直喊「别走神」）。"
    )
    for note in state.get("notes") or []:
        lines.append(f"【玩家反馈】{note}")
    lines.append(_context_device_rules(state))
    return "\n".join(lines)


def parse_llm_json(content: str) -> dict:
    """Accept only a whole object or whole fenced object, never nested fragments."""
    text = content.lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {}


def _final_content(message: dict) -> str:
    """Never interpret reasoning_content as the model's final words or actions."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(part["text"] for part in content if isinstance(part, dict)
                       and part.get("type") in ("text", "output_text")
                       and isinstance(part.get("text"), str)).strip()
    return ""


def _unsupported_json_format(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    error = response.text[:4000].lower()
    return ("response_format" in error or "json_object" in error) and any(
        phrase in error for phrase in ("not supported", "unsupported", "unknown", "unrecognized", "不支持")
    )


def _safe_finish_reason(value):
    allowed = {
        "stop", "length", "content_filter", "tool_calls", "function_call",
        "aborted", "insufficient_system_resource", None,
    }
    return value if isinstance(value, (str, type(None))) and value in allowed else "unknown"


def _response_diagnostic(data: dict, choice: dict, content: str, *, attempt: int, json_mode: bool) -> dict:
    """Keep only bounded response metadata, never provider text or identifiers."""
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}

    def count(value):
        return value if type(value) is int and 0 <= value <= 10_000_000 else None

    return {
        "attempt": attempt,
        "finish_reason": _safe_finish_reason(choice.get("finish_reason")),
        "completion_tokens": count(usage.get("completion_tokens")),
        "reasoning_tokens": count(details.get("reasoning_tokens")),
        "empty_content": not bool(content),
        "json_mode": json_mode,
    }


class ModelResponseError(RuntimeError):
    """Safe response diagnostics without provider payloads, prompts or keys."""
    def __init__(self, message: str, *, code: str, finish_reason=None, attempts=1, response_attempts=None):
        super().__init__(message)
        self.diagnostic = {
            "code": code, "attempts": attempts,
            "finish_reason": _safe_finish_reason(finish_reason),
        }
        if response_attempts:
            self.diagnostic["response_attempts"] = [dict(item) for item in response_attempts]


def _require_ascii_key(key: str) -> None:
    """密钥必须纯 ASCII：示例配置里的中文占位符会作为 Authorization 头把请求整体崩掉。"""
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        raise RuntimeError(
            "API Key 无效：包含中文或特殊字符（疑似示例占位符），请在「设置 → AI 模型配置」填入真实密钥"
        ) from None




class LLM:
    def __init__(self, cfg) -> None:
        llm_cfg = cfg["llm"]
        base = llm_cfg["base_url"].rstrip("/")
        self.url = f"{base}/chat/completions"
        self.api_key = llm_cfg["api_key"]
        self.model = llm_cfg["model"]
        self.temperature = float(llm_cfg["temperature"])
        self.max_tokens = int(llm_cfg["max_tokens"])
        self.json_mode = bool(llm_cfg.get("json_mode", True))
        # 默认绕过系统代理直连（DeepSeek 是国内服务，走代理反而被梯子抽风拖断）；
        # 中转站在境外、确需代理时配置 llm.trust_env: true 恢复读系统代理
        self.trust_env = bool(llm_cfg.get("trust_env", False))
        self.timeout_s = max(1.0, float(llm_cfg["timeout_s"]))
        self.client = httpx.AsyncClient(timeout=self.timeout_s, trust_env=self.trust_env)
        # Official Flash defaults to high-effort thinking. Interactive device
        # dialogue uses its documented non-thinking mode; custom hosts/models
        # receive no provider-specific parameters.
        self.is_deepseek_official = urlsplit(base).hostname == "api.deepseek.com"
        self.interactive_flash = self.is_deepseek_official and self.model in {
            "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
        }

        # 视觉任务独立端点（如本地 Ollama 的 Qwen2.5-VL）；留空则与主模型相同
        v = llm_cfg.get("vision") or {}
        v_base = str(v.get("base_url") or "").strip()
        v_model = str(v.get("model") or "").strip()
        self.vision_url = (
            f"{v_base.rstrip('/')}/chat/completions" if v_base else self.url
        )
        self.vision_api_key = str(v.get("api_key") or "").strip() or self.api_key
        self.vision_model = v_model or self.model
        self.vision_timeout = float(v.get("timeout_s", llm_cfg.get("timeout_s", 60)))

    async def chat(
        self,
        character: dict,
        messages: list[dict],
        state: dict,
        image_b64: str | None = None,
    ) -> tuple[str, list]:
        """返回 (台词, actions 列表)。image_b64 提供时附加到最新一条用户消息。"""
        if state.get("control_device") is False:
            image_b64 = None
        system = build_system_prompt(character, state)
        payload_messages = []
        for message in messages:
            content = message["content"]
            if (self.is_deepseek_official and self.json_mode
                    and message["role"] == "assistant" and isinstance(content, str)):
                # GameLoop stores only displayed words. Keep those words intact
                # while demonstrating the same JSON contract as the next reply;
                # never infer or replay historical actions from their text.
                content = json.dumps({"line": content, "actions": []}, ensure_ascii=False)
            payload_messages.append({"role": message["role"], "content": content})
        if image_b64:
            # 把画面附加到最后一条 user 消息（多模态 content 格式）
            for msg in reversed(payload_messages):
                if msg["role"] == "user":
                    msg["content"] = [
                        {"type": "text", "text": msg["content"]},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}"
                            },
                        },
                    ]
                    break
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + payload_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # 部分模型不支持 response_format 参数；json_mode=false 时走文本+兜底解析
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            _require_ascii_key(self.api_key)
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.interactive_flash:
            payload["thinking"] = {"type": "disabled"}
        logger.debug("调用模型 %s", self.model)
        response_attempts = []
        # One wall-clock deadline covers all HTTP I/O and the optional repair.
        # httpx read timeout alone resets as bytes arrive and used to multiply
        # across up to four sequential completions.
        async with asyncio.timeout(self.timeout_s):
            for attempt in range(2):
                resp = await self.client.post(self.url, headers=headers, json=payload)
                if attempt == 0 and payload.get("response_format") and _unsupported_json_format(resp):
                    payload.pop("response_format", None)
                    continue
                resp.raise_for_status()
                try:
                    data = resp.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                    if not isinstance(message, dict):
                        raise TypeError("message")
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError("模型接口返回了无法识别的响应结构") from exc
                content = _final_content(message)
                if self.is_deepseek_official:
                    response_attempts.append(_response_diagnostic(
                        data, choice, content, attempt=attempt+1,
                        json_mode=bool(payload.get("response_format")),
                    ))
                    logger.info("DeepSeek response metadata: %s", response_attempts)
                if choice.get("finish_reason") == "length":
                    raise ModelResponseError("模型回复因输出长度上限而截断，本轮未执行动作；请提高最大输出长度或缩短回复",
                                             code="truncated", finish_reason="length", attempts=attempt+1,
                                             response_attempts=response_attempts)
                if choice.get("finish_reason") == "content_filter" or message.get("refusal"):
                    raise RuntimeError("模型未能提供这次回复，请调整对话内容后重试")
                if choice.get("finish_reason") in ("aborted", "insufficient_system_resource"):
                    reason = "推理服务资源不足" if choice["finish_reason"] == "insufficient_system_resource" else "生成被中断"
                    raise ModelResponseError(f"模型{reason}，本轮未执行动作；请稍后重试",
                                             code="interrupted", finish_reason=choice["finish_reason"], attempts=attempt+1,
                                             response_attempts=response_attempts)
                parsed = parse_llm_json(content)
                line = parsed.get("line")
                line = line.strip() if isinstance(line, str) else ""
                if line:
                    actions = parsed.get("actions")
                    if not isinstance(actions, list) or state.get("control_device") is False:
                        actions = []
                    return line, actions
                if content and not self.json_mode and not parsed:
                    return content, []
                if attempt == 0:
                    # Only an unusable final response is repaired. No device
                    # action has been issued, and no HTTP/timeout is retried.
                    if self.is_deepseek_official and not content and payload.get("response_format"):
                        # DeepSeek documents occasional empty JSON-mode output.
                        # Drop the wire format only: strict final JSON parsing
                        # stays enabled via self.json_mode for this whole turn.
                        payload.pop("response_format", None)
                    payload["messages"][0]["content"] += (
                        '\n请只返回一个完整 JSON 对象：{"line":"非空的角色回复","actions":[]}。'
                        "不要返回思考过程或多个 JSON 对象。"
                    )
                    continue
                if not content:
                    reason = "只有推理内容、没有最终回复" if message.get("reasoning_content") else "content 与 reasoning_content 均为空"
                    raise ModelResponseError(f"模型连续返回空的最终回复（{reason}），本轮未执行动作；请稍后重试或检查模型设置",
                                             code="empty_content", finish_reason=choice.get("finish_reason"), attempts=attempt+1,
                                             response_attempts=response_attempts)
                raise ModelResponseError("模型没有返回有效的最终回复（需要非空 line），本轮未执行动作",
                                         code="invalid_response", finish_reason=choice.get("finish_reason"), attempts=attempt+1,
                                         response_attempts=response_attempts)
        raise RuntimeError("模型未返回有效回复")

    async def describe_image(
        self,
        image_b64: str,
        instruction: str,
        image_ext: str = "jpg",
        system: str | None = None,
    ) -> str:
        """看图转文（素材整理用）：图片 + 指令 -> 纯文本描述，不要求 JSON。"""
        ext = image_ext.lower().lstrip(".")
        if ext in ("jpg",):
            ext = "jpeg"
        if ext not in ("jpeg", "png", "webp", "gif"):
            ext = "jpeg"
        payload = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "system",
                    "content": system or "你是素材整理助手：按要求观察图片并输出文本，不要输出 JSON。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{ext};base64,{image_b64}"
                            },
                        },
                    ],
                },
            ],
            "max_tokens": 4000,
        }
        headers = {"Content-Type": "application/json"}
        if self.vision_api_key:
            _require_ascii_key(self.vision_api_key)
            headers["Authorization"] = f"Bearer {self.vision_api_key}"

        logger.debug("调用视觉端点描述图片: %s", self.vision_model)
        resp = await self.client.post(self.vision_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        message = data["choices"][0]["message"]
        # 推理型模型：content 可能为空，答案会落在 reasoning_content 里
        content = str(message.get("content") or "").strip()
        if not content:
            content = str(message.get("reasoning_content") or "").strip()
        return content

    async def complete(self, system: str, user: str, max_tokens: int = 4000,
                       timeout: float | None = None) -> str:
        """纯文本补全（风格蒸馏等非 JSON 任务用）。timeout 给大任务延长（秒）。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        client = self.client
        if timeout:
            client = httpx.AsyncClient(timeout=timeout)
        try:
            resp = await client.post(self.url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            message = data["choices"][0]["message"]
            content = str(message.get("content") or "").strip()
            if not content:
                content = str(message.get("reasoning_content") or "").strip()
            return content
        finally:
            if timeout:
                await client.aclose()
