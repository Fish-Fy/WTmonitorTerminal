"""载具记忆器 (Roster)：从 /hudmsg 文案里记住"谁开什么载具、现在死没死"，并推出敌友。

为什么能从文本推敌友: 8111 不给阵营字段(damage 里 enemy 恒 false)、也不给"哪个是我"，
但真人之间的击杀文案有「甲 (载具) 击落了 乙 (载具)」结构。一条 甲→乙 的交战就意味**两端异营**，
于是全部记录构成一张图；只要用户给过一次自己的昵称，从我做 BFS 二分染色即可解出敌/友，
而且与消息到达顺序无关(所以这里存边、查询时解算，而不是增量地猜)。

记忆范围: **只在本局有效**(用户要求去掉跨局累积) —— 阵营是推导结果、识别不落盘；换图由
MainWindow 重建一个空 Roster，要留档只能手动"导出"成人读 txt。

存活判定只用**可证实的时间序证据**，没有证据就是"状态未知"，绝不默认存活：
    actor 出现在交战行 / 拿嘉奖     → 该时刻存活(在开火)
    victim 被"重创/致命攻击/引燃"   → 该时刻仍存活(只是受伤)
    victim 被"击落/击毁/击沉"       → 该时刻阵亡
    「X (机型) 已坠毁。」           → 该时刻阵亡
    「X 已掉线。」                   → 该时刻**离场**(注意: 真机上它几乎总紧跟一次阵亡,
                                      只代表"此刻不在战场", 不等于退出游戏, 之后还可翻回存活)
取三者时间序最新的那条作为当前状态。单人句(掉线/坠毁/嘉奖)**只更新已存在的人**，
不凭空把没交过手的人加进名单。

计数口径: `vehicles[model]` 是**提及次数**，不是击杀数 —— 同一条击杀链会连发
「致命攻击 / 重创 / 的攻击引燃了 / 击落了」多条文案，四句都会记一次机型。载具**集合**不受影响；
真要"我打下来几架"看 `stats(name)["killed_by_me"]`（只数击杀动词）。
AI、任务目标和弹药不进入名单，也不参与阵营推导和名单击杀数。混合事件仍记录真人一侧的状态和载具。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..i18n import tr
from .hudmsg import EN_KILL_VERBS, HudEvent, clean_text

# 动词分类: 击杀类 / 伤害类(不算击杀, 但同样是"两端异营"的边)
_KILL_VERBS: tuple[str, ...] = ("击落了", "击毁了", "击沉了", "击杀了", "消灭了")
_DAMAGE_VERBS: tuple[str, ...] = ("重创", "致命攻击", "引燃了", "击伤了", "攻击了")
# 单人句(只有主语)的状态短语: 短语 -> kind
_STATE_PHRASES: tuple[tuple[str, str], ...] = (
    ("已掉线", "left"), ("已坠毁", "crashed"),
    ("已退出战斗", "quit"), ("退出了战斗", "quit"), ("已离开战斗", "quit"), ("离开了战斗", "quit"),
    ("先拔头筹", "first_blood"),                       # 本局第一次击杀 => 开局标志
    ("最后一击", "last_kill"), ("最终赢家", "last_kill"), ("完成了一击", "last_kill"),
    ("获得嘉奖", "award"), ("杀！", "award"),
)
_END_AWARDS = frozenset(("最后一击", "最终赢家", "完成了一击", "最佳小队",
                         "final blow", "last blow", "best squad"))

_VERB_RE = "|".join(re.escape(v) for v in (*_KILL_VERBS, *_DAMAGE_VERBS))
# 取最左边的动词(同一位置上击杀类优先, 因为 alternation 里击杀动词排在前面)
_VERB_FIND_RE = re.compile(_VERB_RE)
_STATE_FIND_RE = re.compile("|".join(re.escape(p) for p, _k in _STATE_PHRASES))
_STATE_KIND = dict(_STATE_PHRASES)
_EN_DAMAGE = ("critically damaged", "severely damaged", "set afire", "set on fire", "damaged")
_EN_ACTIVE = re.compile(
    r"(?<= )(?P<verb>" + "|".join((*EN_KILL_VERBS, *_EN_DAMAGE)) + r")(?= )",
    re.IGNORECASE,
)
_EN_PASSIVE = re.compile(
    r"(?<= )(?:(?:has been|was) )?(?P<verb>" + "|".join((*EN_KILL_VERBS, *_EN_DAMAGE))
    + r") by(?= )", re.IGNORECASE,
)
_EN_SINGLE = re.compile(
    r"^(?P<subject>.+?) (?:(?P<left>has disconnected from the game)"
    r"|(?P<quit>has quit the battle|has left the battle)"
    r"|(?P<crashed>has crashed)|(?P<first_blood>has delivered the first strike)"
    r"|(?P<last_kill>has delivered the final blow)"
    r"""|(?P<award>has achieved ["“][^"”]+["”]))[.!]?$""",
    re.IGNORECASE,
)
_AWARD_TITLE = re.compile(r'''["“「](?P<title>[^"”」]+)["”」][.!。！]?$''')
_FIXED_KILL_AWARDS = {"双杀": 2, "三杀": 3, "double strike": 2, "triple strike": 3}
_NUMBERED_KILL_AWARD = re.compile(
    r"(?:(?:空中|地面|水面|海上)载具\s*([0-9]{1,4})\s*连杀"
    r"|(?:连杀|多杀|连续无伤歼敌|(?:(?:ground|water) )?multi strike|shadow strike streak)"
    r"\s*[:：]?\s*[×x]\s*([0-9]{1,4}))",
    re.IGNORECASE,
)
# 操作数里的 "(载具)": 名字本身不含圆括号(战队前缀用 []^=╍╎ 等), 且真实文案存在
# "A (机型) 的**攻击引燃了** B (机型)" 这种动词带前缀填充的情况, 所以先按动词切两段,
# 再在每段里取最后一段 (...) 作载具、其之前的部分作名字。
_KILL_SET = frozenset(_KILL_VERBS)
_SPACE_RE = re.compile(r"\s+")
# 名字边界: 战队前缀与昵称之间必须是"非文字"字符, 否则 ExamplePilot2 会被当成我
_WORD_RE = re.compile(r"[0-9A-Za-z一-鿿]")

# 状态键 -> 中文标签(面板与导出共用)
# killed 显示为「重生中」(用户要求): 被击毁的人不消失, 等重生; 明确离场(已掉线)才隐藏
STATUS_LABELS: dict[str, str] = {"alive": "存活", "killed": "重生中", "left": "离场", "unknown": "状态未知"}
TEAM_LABELS = {"enemy": "敌方", "friend": "我方"}
STATUS_ORDER: tuple[str, ...] = ("alive", "killed", "left", "unknown")


def normalize_key(name: str) -> str:
    """归一化: 去不可见字符、去首尾空白、折叠内部空白、转小写。"""
    return _SPACE_RE.sub(" ", clean_text(name or "")).strip().lower()


# 战队前缀的写法: ^DEMO^ / [ABC] / =XYZ= / ╍ABC╍ / ╎ABC╎(后两个是战雷特殊符号战队)
_CLAN_PREFIX_RE = re.compile(
    r"^(?:\^[^^]{1,24}\^|\[[^\[\]]{1,24}\]|=+[^=]{1,24}=+|╍[^╍]{1,24}╍|╎[^╎]{1,24}╎)\s*")
NAME_LIMIT = 24         # 显示名总字符上限(用户 2026-10-11 二次定稿: **联队名也计入**)


def split_clan(name: str) -> tuple[str, str]:
    """拆成 (战队前缀含其后的空格, 昵称)。名字列已改为整体截断, 这里留给别处/测试用。"""
    text = clean_text(name or "").strip()
    m = _CLAN_PREFIX_RE.match(text)
    if not m:
        return "", text
    return m.group(0), text[m.end():]


def short_name(name: str, limit: int = NAME_LIMIT, tail: str = "-") -> str:
    """面板显示用的短名: **整个名字(含战队前缀)统一截到 limit 个字符**, 超出以 tail 结尾。

    用户两次口径(2026-10-11): 先要"昵称 8 字、联队名不计数", 实测后改成
    "**统一计入联队名称, 总共 24 字符**" ⇒ 不再给前缀豁免, ^DEMO^ 那 6 个字符也占额度。
    计数按 Unicode 字符(CJK 一个字算一个); 全名始终留在 tooltip 里。
    """
    text = clean_text(name or "").strip()
    n = max(1, int(limit))
    if len(text) <= n:
        return text
    return f"{text[:n]}{tail}"


def is_self(name: str, my_nick: str) -> bool:
    """判断文案里的名字是不是"我": 忽略大小写/不可见符/战队前缀。"""
    a, b = normalize_key(name), normalize_key(my_nick)
    if not a or not b:
        return False
    if a == b:
        return True
    return _suffix_match(a, b) or _suffix_match(b, a)


def _suffix_match(long: str, short: str) -> bool:
    """long 以 short 结尾, 且 short 前面是非文字字符(战队前缀的分隔处)。"""
    if len(long) <= len(short) or not long.endswith(short):
        return False
    prev = long[-(len(short) + 1)]
    return not _WORD_RE.match(prev)


def _is_player_operand(name: str | None, vehicle: str | None) -> bool:
    # HUD 用「昵称 (载具)」显示玩家。AI、任务目标和弹药通常只有目标名，
    # 即使击毁时带 AI 前缀，受伤时也可能只出现机型，例如「米格-15bis ISh」。
    if not name or not vehicle:
        return False
    name_key, vehicle_key = normalize_key(name), normalize_key(vehicle)
    return (not name_key.startswith("weapons/") and not vehicle_key.startswith("weapons/")
            and name_key not in ("ai", "[ai]", f"ai{vehicle_key}", f"ai {vehicle_key}"))


@dataclass
class CombatEvent:
    kind: str                       # kill / damage / left / crashed / award / other
    actor: str | None = None
    actor_vehicle: str | None = None
    verb: str = ""
    victim: str | None = None
    victim_vehicle: str | None = None
    subject: str | None = None      # 单人句的主角(掉线/坠毁/嘉奖行)
    subject_vehicle: str | None = None
    time: float = 0.0
    raw: str = ""
    award_name: str = ""
    award_streak: int = 0

    @property
    def is_lethal(self) -> bool:
        return self.kind == "kill"

    @property
    def is_pair(self) -> bool:
        return bool(self.actor and self.victim)

    @property
    def actor_is_player(self) -> bool:
        return _is_player_operand(self.actor, self.actor_vehicle)

    @property
    def victim_is_player(self) -> bool:
        return _is_player_operand(self.victim, self.victim_vehicle)

    @property
    def is_player_pair(self) -> bool:
        return self.actor_is_player and self.victim_is_player

    @property
    def player_death_name(self) -> str | None:
        if self.kind == "kill" and self.victim_is_player:
            return self.victim
        if self.kind == "crashed" and _is_player_operand(self.subject, self.subject_vehicle):
            return self.subject
        return None


def _squash(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


# 载具 = 操作数里最后一段 "(...)"; veh 用贪婪 .* 是为了能吃下 "台风 (AESA)" 这种自带括号的机型名
_VEH_TAIL_RE = re.compile(r"^(?P<name>.*?)\((?P<veh>.*)\)[^()]*$")


def _split_operand(chunk: str) -> tuple[str | None, str | None]:
    """把 "名字 (载具) [动词前缀填充]" 切成 (名字, 载具); 没括号就整段是名字。"""
    text = _squash(chunk)
    if not text:
        return None, None
    m = _VEH_TAIL_RE.match(text)
    if m:
        return (_squash(m.group("name")) or None), (_squash(m.group("veh")) or None)
    return text, None


def _parse_award(text: str) -> tuple[str, int]:
    match = _AWARD_TITLE.search(text)
    if not match:
        return "", 0
    title = match["title"].strip()
    name = _award_key(title)
    numbered = _NUMBERED_KILL_AWARD.fullmatch(name)
    if numbered:
        return title, int(numbered[1] or numbered[2])
    return title, _FIXED_KILL_AWARDS.get(name, 0)


def _award_key(title: str) -> str:
    return title.rstrip("!.！。").rstrip().casefold()


def _parse_english_line(text: str) -> CombatEvent | None:
    for pattern, reverse in ((_EN_PASSIVE, True), (_EN_ACTIVE, False)):
        for match in pattern.finditer(text):
            left, right = text[:match.start()], text[match.end():]
            # A verb inside a vehicle name is never the separator.
            if left.count("(") != left.count(")") or right.count("(") != right.count(")"):
                continue
            if reverse:
                left, right = right, left
            actor, actor_vehicle = _split_operand(left)
            victim, victim_vehicle = _split_operand(right.rstrip(".!"))
            if actor and victim:
                verb = match["verb"].lower()
                return CombatEvent("kill" if verb in EN_KILL_VERBS else "damage",
                                   actor=actor, actor_vehicle=actor_vehicle, verb=verb,
                                   victim=victim, victim_vehicle=victim_vehicle, raw=text)
    match = _EN_SINGLE.fullmatch(text)
    if match:
        subject, vehicle = _split_operand(match["subject"])
        award, streak = _parse_award(match["award"] or "")
        kind = "last_kill" if _award_key(award) in _END_AWARDS else match.lastgroup
        return CombatEvent(kind, subject=subject, subject_vehicle=vehicle,
                           verb=match[match.lastgroup], raw=text, award_name=award, award_streak=streak)
    return None


def parse_line(text: str) -> CombatEvent | None:
    """一行 HUD 文案 -> 事件。两方交战句优先; 否则识别单人句(掉线/坠毁/嘉奖); 都不像则 other。

    「发动机过热」「油温过高」「xxxtd! kd?NET_PLAYER_DISCONNECT_FROM_GAME」这类无人称噪声
    不匹配任何短语 -> kind=other, subject=None, 对名单零影响。
    """
    if not text:
        return None
    cleaned = _squash(clean_text(text))
    m = _VERB_FIND_RE.search(cleaned)
    if m:
        actor, actor_vehicle = _split_operand(cleaned[:m.start()])
        victim, victim_vehicle = _split_operand(cleaned[m.end():])
        if actor and victim:
            verb = m.group(0)
            return CombatEvent(
                kind="kill" if verb in _KILL_SET else "damage",
                actor=actor, actor_vehicle=actor_vehicle, verb=verb,
                victim=victim, victim_vehicle=victim_vehicle, raw=cleaned)
    s = _STATE_FIND_RE.search(cleaned)
    if s:
        subject, subject_vehicle = _split_operand(cleaned[:s.start()])
        kind = _STATE_KIND[s.group(0)]
        award, streak = _parse_award(cleaned[s.end():]) if kind == "award" else ("", 0)
        if _award_key(award) in _END_AWARDS:
            kind = "last_kill"
        return CombatEvent(kind=kind, subject=subject,
                           subject_vehicle=subject_vehicle, verb=s.group(0), raw=cleaned,
                           award_name=award, award_streak=streak)
    return _parse_english_line(cleaned) or CombatEvent(kind="other", raw=cleaned)


@dataclass
class _Entry:
    display: str
    vehicles: dict[str, int] = field(default_factory=dict)
    killed_by_me: int = 0
    killed_me: int = 0
    seen: int = 0
    last_time: float = 0.0
    # (局内秒, 到达序号) 证据时刻; 空元组 = 无该类证据。用序号兜住 time 缺失/全 0 的情况。
    alive_at: tuple[float, int] = ()
    dead_at: tuple[float, int] = ()
    left_at: tuple[float, int] = ()
    # K-2: 击杀按"该玩家作为 击落/击毁/击沉 主语"统计, 并按其当时载具分桶
    kills: dict[str, int] = field(default_factory=dict)
    used_at: dict[str, tuple[float, int]] = field(default_factory=dict)   # 载具 -> 最近证据


class Roster:
    """本局的"人-载具-阵营-存活"档案。"""

    def __init__(self, my_nick: str = ""):
        self.my_nick = (my_nick or "").strip()
        self._entries: dict[str, _Entry] = {}
        self._edges: dict[tuple[str, str, str], int] = {}
        self._pins: dict[str, int] = {}                # 外部证据(击杀↔图标消失关联): key -> ±1
        self._camps: dict[str, int] | None = None      # 1=与我同营, -1=敌
        self._seq = 0                                   # 事件到达序, 用于时间序比较
        self._kill_seen: set[tuple[str, str, float]] = set()   # 同一次击杀的多条文案去重
        self._self_vehicle: str | None = None
        # 我本人从**第一秒**就在名单里: 接口不给玩家列表, 一局里没人提到我时面板上会连我都没有
        # (用户报的"有时把我本人的数据去掉了")。
        if self.my_nick:
            me = self._touch(normalize_key(self.my_nick), self.my_nick, 0.0)
            self._mark(me, "alive", 0.0, 1)

    # ---------------------------------------------------------- 采集
    def set_my_nick(self, nickname: str) -> None:
        """中途填写/纠正昵称时重新计算本人关系，保留本局名单与状态。"""
        self.my_nick = nickname.strip()
        self._camps = None
        self._pins.clear()
        for entry in self._entries.values():
            entry.display = self._preferred_display(entry.display)
            entry.killed_me = entry.killed_by_me = 0
        for actor, victim, _time in self._kill_seen:
            if self.is_self_name(self._entries[actor].display):
                self._entries[victim].killed_by_me += 1
            if self.is_self_name(self._entries[victim].display):
                self._entries[actor].killed_me += 1
        if self.my_nick and not any(self.is_self_name(e.display) for e in self._entries.values()):
            me = self._touch(normalize_key(self.my_nick), self.my_nick, 0.0)
            self._mark(me, "alive", 0.0, self._seq)

    def absorb_events(self, events: Iterable[HudEvent]) -> int:
        """返回"改动了名单"的事件数(供 UI 判断要不要刷新)。"""
        return sum(1 for e in events if self.absorb_line(e.msg, e.time))

    def absorb_line(self, msg: str, t: float = 0.0) -> bool:
        ev = parse_line(msg)
        if ev is None or ev.kind == "other":
            return False
        ev.time = t or ev.time
        return self.absorb(ev)

    def absorb(self, ev: CombatEvent) -> bool:
        s = self._seq + 1
        t = float(ev.time or 0.0)
        if ev.is_pair:
            changed = self._record_pair(ev, t, s)
        elif ev.subject:
            changed = self._record_single(ev, t, s)
        else:
            changed = False
        if changed:
            self._seq = s
        return changed

    def _record_pair(self, ev: CombatEvent, t: float, s: int) -> bool:
        assert ev.actor and ev.victim
        actor_is_player, victim_is_player = ev.actor_is_player, ev.victim_is_player
        ka, kv = normalize_key(ev.actor), normalize_key(ev.victim)
        if actor_is_player:
            self._record_participant(ka, ev.actor, ev.actor_vehicle, "alive", t, s)
        if victim_is_player:
            self._record_participant(kv, ev.victim, ev.victim_vehicle,
                                     "dead" if ev.is_lethal else "alive", t, s)
        if not (actor_is_player and victim_is_player):
            return actor_is_player or victim_is_player
        key = (ka, kv, ev.kind)
        self._edges[key] = self._edges.get(key, 0) + 1
        if ev.is_lethal:
            # 真机同一次击杀会发两条文案(文案相同、id 不同) => 按 (击杀者,被击杀者,时间) 只算一次
            dk = (ka, kv, t)
            if dk not in self._kill_seen:
                self._kill_seen.add(dk)
                veh = ev.actor_vehicle or "未知载具"
                ea, e_v = self._entries[ka], self._entries[kv]
                ea.kills[veh] = ea.kills.get(veh, 0) + 1
                if self.is_self_name(ev.actor):
                    e_v.killed_by_me += 1
                if self.is_self_name(ev.victim):
                    ea.killed_me += 1
        self._camps = None                              # 图变了, 染色缓存作废
        return True

    def _record_participant(self, key: str, name: str, vehicle: str | None,
                            state: str, t: float, s: int) -> None:
        entry = self._touch(key, name, t)
        assert vehicle
        entry.vehicles[vehicle] = entry.vehicles.get(vehicle, 0) + 1
        entry.used_at[vehicle] = (t, s)
        self._mark(entry, state, t, s)

    def _record_single(self, ev: CombatEvent, t: float, s: int) -> bool:
        assert ev.subject is not None
        e = self._entries.get(normalize_key(ev.subject))
        if e is None:
            return False                               # 单人句不扩名单
        if ev.subject_vehicle:
            e.vehicles[ev.subject_vehicle] = e.vehicles.get(ev.subject_vehicle, 0) + 1
            e.used_at[ev.subject_vehicle] = (t, s)
        # 先拔头筹/最后一击/嘉奖 = 此刻还活着; 已坠毁=阵亡; 已掉线=离场
        self._mark(e, {"left": "left", "quit": "left", "crashed": "dead"}.get(ev.kind, "alive"), t, s)
        return True

    @staticmethod
    def _mark(entry: _Entry, state: str, t: float, s: int) -> None:
        """越新的证据覆盖越旧的(用 (局内秒, 到达序) 比较, time 缺失时退化成到达序)。"""
        attr = {"alive": "alive_at", "dead": "dead_at", "left": "left_at"}[state]
        cur = getattr(entry, attr)
        if not cur or (t, s) >= cur:
            setattr(entry, attr, (t, s))

    def _preferred_display(self, display: str) -> str:
        """本人昵称用输入时的大小写，保留文案中的战队前缀；匹配键不变。"""
        clan, name = split_clan(display)
        _configured_clan, preferred = split_clan(self.my_nick)
        if preferred and normalize_key(name) == normalize_key(preferred):
            return clan + preferred
        return display.strip()

    def _touch(self, key: str, display: str, t: float) -> _Entry:
        e = self._entries.get(key)
        if e is None:
            e = self._entries[key] = _Entry(display=self._preferred_display(display))
        e.seen += 1
        e.last_time = max(e.last_time, t or 0.0)
        return e

    # ---------------------------------------------------------- 阵营解算
    def pin_camp(self, name: str, camp: int) -> bool:
        """外部证据(击杀文案 ↔ 地图图标消失关联)钉一个阵营: camp +1=我方 / -1=敌方。

        铁律: 优先级**低于**从"我"推出的图染色(_resolve 里图先 BFS), pin 不得翻转图;
        同人正反冲突保留先到; 我本人永不被钉; 只钉名单里已有的人。返回是否采纳。
        """
        if not name or self.is_self_name(name):
            return False
        key = normalize_key(name)
        if key not in self._entries or key in self._pins:
            return False
        self._pins[key] = 1 if camp > 0 else -1
        self._camps = None                              # 染色缓存作废
        return True

    def _resolve(self) -> dict[str, int]:
        if self._camps is not None:
            return self._camps
        camps: dict[str, int] = {}
        me = normalize_key(self.my_nick)
        adj: dict[str, list[str]] = {}
        for a, b, _kind in self._edges:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)

        def bfs(queue: list[str]) -> None:
            while queue:
                cur = queue.pop(0)
                for nxt in adj.get(cur, []):
                    if nxt not in camps:
                        camps[nxt] = -camps[cur]        # 交战两端异营; 冲突保留先到解, 不翻转
                        queue.append(nxt)

        # 第一优先: 所有被判为"我"的种子
        seeds = [k for k in self._entries if k == me or is_self(self._entries[k].display, self.my_nick)]
        for s in seeds:
            camps[s] = 1
        bfs(list(seeds))
        # 第二优先: 外部 pin(地图图标关联) —— 只染图还没覆盖到的连通分量
        pinq: list[str] = []
        for k, c in self._pins.items():
            if k not in camps:
                camps[k] = c
                pinq.append(k)
        bfs(pinq)
        self._camps = camps
        return camps

    # ---------------------------------------------------------- 归属分类
    def is_self_name(self, name: str | None) -> bool:
        """没配昵称时永远 False —— 不凭空造归属。"""
        return bool(self.my_nick) and is_self(name or "", self.my_nick)

    def classify(self, ev_or_text) -> str:
        """这条交战对"我"而言是什么: self_action / self_victim / ally_action / enemy_action / unknown。

        只看发起方阵营: 图上推不出关系的人一律 unknown(宁可不知道, 也不猜成敌方)。
        """
        ev = parse_line(ev_or_text) if isinstance(ev_or_text, str) else ev_or_text
        if ev is None or not ev.is_pair:
            return "unknown"
        if self.is_self_name(ev.actor):
            return "self_action"
        if self.is_self_name(ev.victim):
            return "self_victim"
        side = self.side_of(ev.actor or "")
        if side == "friend":
            return "ally_action"
        if side == "enemy":
            return "enemy_action"
        return "unknown"

    def _side_for(self, key: str, display: str, camps: dict[str, int]) -> str:
        if self.my_nick and is_self(display, self.my_nick):
            return "me"
        camp = camps.get(key)
        if camp == 1:
            return "friend"
        if camp == -1:
            return "enemy"
        return "unknown"

    def side_of(self, name: str) -> str:
        camps = self._resolve()
        key = normalize_key(name)
        entry = self._entries.get(key)
        if entry is None:
            return "me" if (self.my_nick and is_self(name, self.my_nick)) else "unknown"
        return self._side_for(key, entry.display, camps)

    # ---------------------------------------------------------- 存活状态
    def status_of(self, name: str) -> str:
        """alive / killed / left / unknown —— 取三类证据里时间序最新的那条, 没证据就 unknown。"""
        e = self._entries.get(normalize_key(name))
        if e is None:
            return "unknown"
        return self._status_for(e)

    @staticmethod
    def _status_for(e: _Entry) -> str:
        # 同时刻(如 "A 击落了 B" 里 A 的 alive 与 B 的 dead 同一秒同一序)不跨人比较, 无歧义;
        # 同人多类证据取最新; 完全并列时按 left > killed > alive 保守排序。
        cand = [(e.left_at, "left"), (e.dead_at, "killed"), (e.alive_at, "alive")]
        best_val, best_state = (), "unknown"
        for val, state in cand:
            if not val:
                continue
            if val > best_val or (val == best_val and state != "alive"):
                best_val, best_state = val, state
        return best_state

    def by_status(self, side: str) -> dict[str, dict[str, dict[str, int]]]:
        """某阵营下按存活状态分组(只返回非空组), 供面板细分未定阵营。"""
        camps = self._resolve()
        out: dict[str, dict[str, dict[str, int]]] = {}
        for key, e in self._entries.items():
            if self._side_for(key, e.display, camps) != side:
                continue
            out.setdefault(self._status_for(e), {})[e.display] = dict(e.vehicles)
        return {s: out[s] for s in STATUS_ORDER if s in out}

    def names(self) -> list[str]:
        return [e.display for e in self._entries.values()]

    def self_display_name(self) -> str:
        """显示本人时优先采用文案里带战队前缀的名字。"""
        entries = [e for e in self._entries.values() if self.is_self_name(e.display)]
        if not entries:
            return self.my_nick
        return max(entries, key=lambda e: (bool(split_clan(e.display)[0]), e.last_time,
                                           e.seen, e.display)).display

    def display_names(self) -> list[str]:
        """仅合并显示上的本人别名，原始事件和统计档案保留。"""
        preferred = self.self_display_name()
        return [e.display for e in self._entries.values()
                if not self.is_self_name(e.display) or e.display == preferred]

    def vehicles(self, name: str) -> dict[str, int]:
        e = self._entries.get(normalize_key(name))
        return dict(e.vehicles) if e else {}

    def set_self_vehicle(self, vehicle: str | None) -> bool:
        """用 `/indicators` 的 `type` 兜住"我这局开的是什么"。返回是否发生变化。

        文案里通常根本没有我自己的载具(除非我参与击杀/被击落), 所以本人载具得靠遥测补。
        """
        v = (vehicle or "").strip()
        if not v or not self.my_nick:
            return False
        e = self._entries.get(normalize_key(self.my_nick))
        if e is None:
            return False
        if self._self_vehicle == v and e.used_at.get(v):
            return False
        self._self_vehicle = v
        self._seq += 1
        e.used_at[v] = (float(e.last_time), self._seq)   # 最新序 => current_vehicle 取它
        return True

    # ---------------------------------------------------------- 击杀统计(K-2)
    def kills_by_vehicle(self, name: str) -> dict[str, int]:
        """该玩家作为「击落/击毁/击沉」主语的击杀数, 按其当时使用的载具分桶。"""
        e = self._entries.get(normalize_key(name))
        return dict(e.kills) if e else {}

    def kill_count(self, name: str) -> int:
        e = self._entries.get(normalize_key(name))
        return sum(e.kills.values()) if e else 0

    def used_vehicles(self, name: str) -> list[str]:
        """本局用过的载具, 按最近使用排在前面。"""
        e = self._entries.get(normalize_key(name))
        if not e:
            return []
        return [v for v, _ in sorted(e.used_at.items(), key=lambda kv: kv[1], reverse=True)]

    def current_vehicle(self, name: str) -> str | None:
        used = self.used_vehicles(name)
        return used[0] if used else None

    def stats(self, name: str) -> dict[str, Any]:
        e = self._entries.get(normalize_key(name))
        if not e:
            return {"vehicles": {}, "killed_by_me": 0, "killed_me": 0, "seen": 0,
                    "last_time": 0.0, "status": "unknown", "status_time": 0.0,
                    "kills": {}, "kill_count": 0, "current_vehicle": None}
        st = self._status_for(e)
        t = {"alive": e.alive_at, "killed": e.dead_at, "left": e.left_at}[st][0] if st != "unknown" else 0.0
        return {"vehicles": dict(e.vehicles), "killed_by_me": e.killed_by_me,
                "killed_me": e.killed_me, "seen": e.seen, "last_time": e.last_time,
                "status": st, "status_time": t, "kills": dict(e.kills),
                "kill_count": sum(e.kills.values()), "current_vehicle": self.current_vehicle(name)}

    def roster(self, side: str) -> dict[str, dict[str, int]]:
        camps = self._resolve()
        out: dict[str, dict[str, int]] = {}
        for key, e in self._entries.items():
            if self._side_for(key, e.display, camps) == side:
                out[e.display] = dict(e.vehicles)
        return out

    # ---------------------------------------------------------- 导出(仅本局, 不落盘)
    def _fmt_t(self, t: float) -> str:
        return f"{int(t // 60):02d}:{int(t % 60):02d}" if t else "-"

    def _line_for(self, display: str, *, own: bool = False) -> str:
        """一行 = 昵称 + 击杀数 + 当前载具 + 状态；各载具明细走 `kills_detail()`(悬停)。"""
        st = self.stats(display)
        # 重生中的人当前载具未知: 死时开的那架已是历史, 不再冒充"当前载具"(导出里括注留档)
        veh = st["current_vehicle"] or "?"
        if st["status"] == "killed":
            veh = tr('?（原 {0}）', st['current_vehicle']) if st["current_vehicle"] else "?"
        parts = [tr('击杀 {0}', st['kill_count']), veh, tr(STATUS_LABELS[st["status"]])]
        if st["status"] != "unknown":
            parts[-1] += f"@{self._fmt_t(st['status_time'])}"
        if st["killed_by_me"]:
            parts.append(tr('被我击落{0}', st['killed_by_me']))
        if st["killed_me"]:
            parts.append(tr('击落我{0}', st['killed_me']))
        name = tr('{0}（本人）', display) if own else display
        return f"  {name}  [{' · '.join(parts)}]"

    def kills_detail(self, name: str) -> list[tuple[str, int]]:
        """本局用过的每个载具及其击杀数（只开过没杀过的显示 0）。"""
        used = self.used_vehicles(name)
        kills = self.kills_by_vehicle(name)
        rows = [(v, kills.get(v, 0)) for v in used]
        rows += [(v, n) for v, n in kills.items() if v not in used]
        return sorted(rows, key=lambda kv: (-kv[1], kv[0]))

    def to_text(self) -> str:
        parts: list[str] = []
        displayed = set(self.display_names())
        own = displayed.intersection(self.roster("me"))
        for side, title in TEAM_LABELS.items():
            grouped = displayed.intersection(self.roster(side))
            if side == "friend":
                grouped.update(own)
            if grouped:
                parts.append(f"== {tr(title)} ({len(grouped)}) ==")
                parts += [self._line_for(name, own=name in own) for name in sorted(grouped, key=str.lower)]
        unknown_groups = self.by_status("unknown")
        if unknown_groups:
            total = sum(len(v) for v in unknown_groups.values())
            parts.append(f"== {tr('未定阵营 ({0})', total)} ==")
            for status in STATUS_ORDER:
                rows = unknown_groups.get(status)
                if not rows:
                    continue
                parts.append(f"  -- {tr(STATUS_LABELS[status])} ({len(rows)}) --")
                parts += [self._line_for(d) for d in sorted(rows, key=str.lower)]
        return "\n".join(parts)
