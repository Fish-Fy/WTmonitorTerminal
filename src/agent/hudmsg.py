"""/hudmsg 端点解析 -> HUD 事件流(击杀/坠毁/掉线/嘉奖...)。

真机实测(2026-09-18, 带游标请求)响应形状:
    {"events": [ ... ], "damage": [ {"id":801,"msg":"...","sender":"","enemy":false,
                                      "mode":"","time":396}, ... ]}
要点:
  * 必须带 lastEvt/lastDmg 双游标, 裸 GET 返回 400; id 全局递增, 对局内 time 会随新局归零。
  * 实测 damage 里 mode 恒为 ""、enemy 恒为 false —— 所以**不能**靠 mode 判击杀,
    只能按文案匹配。
  * 战雷在 CJK 字符间插入零宽分隔符(U+200B 等), 任何中文关键词匹配都必须先清洗,
    否则永远匹配不上。msg 存清洗后的文本, 原文保留在 raw。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 不可见标记(用码位写, 源码里不放不可见字符): ZWSP/ZWNJ/ZWJ/WORD JOINER/BOM/LRM/RLM/软连字符
_ZW_RE = re.compile("[" + "".join(chr(c) for c in
                    (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x200E, 0x200F, 0x00AD)) + "]")

# 中文客户端里代表"确认击杀"的片段。注意别把 重创/致命攻击/引燃 这类"伤害"算成击杀。
_KILL_MARKERS_ZH: tuple[str, ...] = ("击落", "击毁", "击沉", "击杀", "消灭")
# 客户端语言未知时的兜底
EN_KILL_VERBS = ("shot down", "destroyed", "killed", "sunk")
_KILL_MARKERS_EN = re.compile(r"\b(?:" + "|".join(EN_KILL_VERBS) + r")\b", re.IGNORECASE)


def clean_text(value: str) -> str:
    """剥掉零宽分隔符(及其他不可见标记), 让文案既可读又能被关键词匹配。"""
    return _ZW_RE.sub("", value)


@dataclass
class HudEvent:
    id: int = 0
    msg: str = ""           # 已清洗
    sender: str = ""
    enemy: bool = False
    mode: str = ""
    time: float = 0.0       # 对局内秒数(新局会归零)
    stream: str = ""        # events / damage / ""(旧形状)
    raw: dict = field(default_factory=dict)

    @property
    def is_kill(self) -> bool:
        mode = (self.mode or "").lower()
        if "kill" in mode or "destroy" in mode:
            return True
        msg = self.msg or ""
        return (any(k in msg for k in _KILL_MARKERS_ZH)
                or bool(_KILL_MARKERS_EN.search(msg)))


def _num(value, cast):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _one(item: dict, stream: str) -> HudEvent:
    def pick(*keys, default=None):
        for k in keys:
            if item.get(k) is not None:
                return item[k]
        return default
    return HudEvent(
        id=_num(pick("id", "Id", "ID"), int) or 0,
        msg=clean_text(str(pick("msg", "message", "text", default="") or "")),
        sender=clean_text(str(pick("sender", "Sender", "name", default="") or "")),
        enemy=bool(pick("enemy", "Enemy", "is_enemy", default=False)),
        mode=str(pick("mode", "Mode", "type", default="") or ""),
        time=_num(pick("time", "Time", "t"), float) or 0.0,
        stream=stream,
        raw=item,
    )


def parse_hudmsg(data) -> list[HudEvent]:
    """接受三种形状: 真机 {events, damage} / 裸数组 / 旧的 {messages: []}。"""
    out: list[HudEvent] = []
    if isinstance(data, dict):
        for stream in ("events", "damage"):
            arr = data.get(stream)
            if isinstance(arr, list):
                out.extend(_one(x, stream) for x in arr if isinstance(x, dict))
        if not out:
            arr = data.get("messages") or data.get("msg") or []
            if isinstance(arr, list):
                out.extend(_one(x, "") for x in arr if isinstance(x, dict))
    elif isinstance(data, list):
        out.extend(_one(x, "") for x in data if isinstance(x, dict))
    return out
