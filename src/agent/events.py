"""击杀/伤害事件的路由：决定"进不进事件面板"与"要不要在地图上画环"。

为什么地图环只画"我被击落": 遥测里**没有**被击杀者的坐标(只有 /map_obj.json 里亮着的单位),
把别人的击杀画在我脚下是假情报。而"我被击落"的位置恰好就是我最后出现的位置 —— 位置可证实。
事件面板显示本人的交战、敌方行动及阵营未知的事件。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..i18n import language, tr
from .roster import CombatEvent, parse_line

_CHINESE_VERBS = {
    "shot down": "击落了", "destroyed": "击毁了", "sunk": "击沉了", "killed": "击杀了",
    "severely damaged": "重创", "critically damaged": "致命攻击",
    "set afire": "引燃了", "set on fire": "引燃了", "damaged": "击伤了",
}


def _who(name: str | None, vehicle: str | None) -> str:
    if not name:
        return "?"
    return f"{name} ({vehicle})" if vehicle else name


def display_verb(verb: str) -> str:
    source_verb = _CHINESE_VERBS.get(verb, verb)
    return verb if language() == "en" and verb in _CHINESE_VERBS else tr(source_verb or "交战")


def first_person(ev: CombatEvent | None, category: str, my_nick: str = "") -> str:
    """把「甲 (载具) 击落了 乙 (载具)」按归属改写成更好读的一句。

    my_nick 只是备用(归属判定在 roster.classify 里已经做过), 保留参数便于独立复用。
    """
    if ev is None:
        return ""
    actor = _who(ev.actor, ev.actor_vehicle)
    victim = _who(ev.victim, ev.victim_vehicle)
    verb = display_verb(ev.verb)
    if category == "self_action":
        return tr("你{0} {1}", verb, victim)
    if category == "self_victim":
        return tr("你被 {0} {1}", actor, verb)
    return f"{actor} {verb} {victim}"


@dataclass(frozen=True)
class FeedItem:
    id: int
    category: str
    kind: str          # kill / damage
    time: float        # 局内秒
    text: str          # 清洗后的原始文案
    fp: str            # 第一人称/第三人称改写
    event: CombatEvent | None = None

    @property
    def display(self) -> str:
        return self.fp or self.text


def build_feed(events: Iterable, roster, settings, seen: set[int] | None = None
               ) -> tuple[list[FeedItem], list]:
    """把 /hudmsg 事件流分流成 (面板行, 需要画环的原始事件)。

    seen 传一个 set 就能跨批次按 id 去重(游标回退/换图重收时不会重复刷屏或重复响)。
    """
    items: list[FeedItem] = []
    rings: list = []
    for e in events or ():
        eid = int(getattr(e, "id", 0) or 0)
        if seen is not None and eid:
            if eid in seen:
                continue
            seen.add(eid)
        ev = parse_line(getattr(e, "msg", "") or "")
        if ev is None or not ev.is_pair or ev.kind not in ("kill", "damage"):
            continue                      # 掉线/嘉奖/机体过热之类的单操作数行不进面板
        cat = roster.classify(ev)
        self_victim = cat == "self_victim"
        if cat == "ally_action" or (self_victim and not getattr(settings, "own_deaths", True)):
            continue
        items.append(FeedItem(id=eid, category=cat, kind=ev.kind,
                              time=float(getattr(e, "time", 0.0) or 0.0),
                              text=ev.raw, fp=first_person(ev, cat), event=ev))
        if self_victim and ev.kind == "kill":
            rings.append(e)
    return items, rings
