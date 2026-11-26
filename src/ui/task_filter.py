"""Shared mode selector for task lists."""
import re

from PySide6.QtWidgets import QComboBox

from ..i18n import language, map_name, tr
from ..agent.task_layout import TASK_MODES

_MISSION_TYPES = {
    "战区控制": "Domination", "战区争夺": "Conquest", "战区攻陷": "Battle",
    "战区防御": "Assault", "战区推进": "Advance", "战线推进": "Advance", "战区突破": "Break",
    "战区割据": "Sector Control", "军事行动": "Operation", "前线作战": "Front Line",
    "夺旗": "Capture the Flag", "护航行动": "Convoy", "护卫行动": "Escort",
    "空中护航": "Air Escort", "对空防御": "Air Assault", "对地攻击": "Ground Strike",
    "空域控制": "Air Domination", "空域争夺": "Air Conquest", "机场控制": "Domination",
    "海域控制": "Naval Domination", "海域争夺": "Naval Conquest", "海域攻陷": "Naval Battle",
    "海上交锋": "Naval Engagement", "停机坪控制": "Apron Control",
    "狭路相逢": "Head-on", "深入打击": "Deep Strike", "纵深防御": "Defense in Depth",
    "防空压制": "SEAD", "绝对控制": "Total Control", "架空历史": "Alternate History",
    "竞技场": "Arena", "枪弹炼狱": "Bullet Hell", "核战冲突": "Nuclear Conflict",
    "飞球大战": "Air Ball", "节日速递": "Holiday Express", "逐梦圣诞节": "Christmas Wish",
    "非洲峡谷": "African Canyon",
}
_MISSION_TYPE = re.compile(r"[\u3400-\u9fff]+")
_LAYOUT_TAG = re.compile(r"布局 (\d+)$")
_DUEL = re.compile(r"^(生死决斗|角斗士) (.+?) - (.+)$")
_VARIANT = re.compile(r"\s*[（(](冬季|小型舰艇|轻型载具)[）)]$")
_VARIANTS = {"冬季": "Winter", "小型舰艇": "Small boats", "轻型载具": "Light vehicles"}
_MISSION_LOCATIONS = {
    "波兰 • 镜湖": "Poland", "波兰 • 原野": "Fields of Poland",
    "许特根森林战役": "Battle of Hürtgen Forest",
    "马其诺防线 • 蒙梅迪防区": "Maginot Line · Montmédy Sector",
    "瓜岛丛林": "Guadalcanal Jungle", "富尔达": "Fulda Gap", "日本": "Japan",
    "东欧 • 欧陆风云": "European Province", "突尼斯 • 荒漠小镇": "Tunisia · Desert Town",
    "新罗西斯克 • 港口之战": "Novorossiysk · Port Battle",
    "第二次阿拉曼战役": "Second Battle of El Alamein",
    "柏林 • 国会大厦": "Berlin · Reichstag", "西奈半岛 • 烽火边城": "Sinai · Border Town",
    "诺曼底 • 奥马哈滩头": "Normandy · Omaha Beach", "意大利 • 坎帕尼亚": "Italy · Campania",
    "沃洛科拉姆斯克 • 雪原之围": "Volokolamsk · Snowfield Siege",
    "极北之地 • 基地": "Arctic · Base", "极北之地 • 栈桥": "Arctic · Pier",
    "突尼斯 • 北非沙漠": "Tunisia · North African Desert", "拉芒什海峡": "English Channel",
    "新几内亚": "New Guinea", "诺曼底 • 深入腹地": "Normandy · Inland",
    "莱州湾": "Laizhou Bay", "登陆北海道演习": "Hokkaido Landing Exercise",
    "不列颠 • 姗姗来迟的船队": "Britain · Late Convoy", "滨海布洛涅": "Boulogne-sur-Mer",
    "比利牛斯山": "Pyrenees", "幽邃峡谷 • 航天港": "Mysterious Valley · Spaceport",
    "克雷姆斯卡亚（1945 年夏）": "Krymsk (Summer 1945)", "伊斯卡拉行动": "Operation Iskra",
    "拉多加 • 冬季（1941 年）": "Ladoga (Winter 1941)", "拉多加湖": "Lake Ladoga",
    "天王星行动": "Operation Uranus", "斯大林格勒保卫战": "Defense of Stalingrad",
    "斯摩棱斯克 • 苏军反攻": "Smolensk · Soviet Counteroffensive",
    "斯摩棱斯克（1941 年）": "Smolensk (1941)", "斯摩棱斯克（1943 年）": "Smolensk (1943)",
    "朝鲜 • 春川战役": "Korea · Battle of Chuncheon", "柏林 • 市郊": "Berlin · Outskirts",
    "柏林 • 市郊（1945 年春）": "Berlin · Outskirts (Spring 1945)",
    "堪察加半岛": "Kamchatka", "堪察加半岛 • 极东之地": "Eastern Kamchatka",
    "火奴鲁鲁": "Honolulu", "瓦胡岛演习": "Oahu Exercise", "科尔松": "Korsun",
    "科尔松 • 战略反攻": "Korsun · Counteroffensive",
    "巴斯托涅": "Bastogne", "巴斯托涅战役": "Battle of Bastogne",
    "莫兹多克 • 冬季（1943 年）": "Mozdok (Winter 1943)", "格罗兹尼": "Grozny",
    "莫尔兹比港": "Port Moresby", "莫斯科 • 42 公里外": "Moscow · 42nd Kilometer",
    "莫斯科 • 纳罗福明斯克": "Moscow · Naro-Fominsk", "莫斯科 • 谢尔普霍夫": "Moscow · Serpukhov",
    "莱茵兰战役": "Battle for the Rhine", "西班牙战役": "Battle for Spain",
    "巩固西西里岛阵线": "Holding the Line in Sicily", "西西里岛": "Sicily", "法国海岸": "French Coast",
    "诺门罕 • 最后一战": "Khalkhin Gol · Final Battle", "帕劳群岛 • 尤水道": "Palau · Yoo Passage",
    "交趾支那地区": "Cochinchina", "越南战场": "Battle for Vietnam", "1946 年国共内战": "Chinese Civil War (1946)",
    "河南": "Henan", "近东": "Near East", "阿拉曼战役": "Battle of El Alamein",
    "登陆马耳他": "Malta Landing", "马耳他之战": "Battle of Malta", "鲁尔 • 埃森": "Ruhr · Essen",
}


def task_mode_combo() -> QComboBox:
    combo = QComboBox()
    for mode, label in (("all", tr("全部模式")), *TASK_MODES.items(), ("other", tr("其他"))):
        combo.addItem(tr(label), mode)
    combo.setToolTip(tr("按游戏模式筛选任务"))
    return combo


def task_choice_name(task) -> str:
    name = task.name
    if len(task.modes) >= 2:
        name = " · ".join(part for part in name.split(" · ") if part not in TASK_MODES.values())
    return mission_name(name)


def _mission_location(location: str) -> str:
    prefix = ""
    if duel := _DUEL.fullmatch(location):
        mode, format_, location = duel.groups()
        prefix = f"{'Duel' if mode == '生死决斗' else 'Gladiators'} {format_.replace('喷气', 'Jets')} - "
    suffix = ""
    if variant := _VARIANT.search(location):
        suffix = f" ({_VARIANTS[variant[1]]})"
        location = location[:variant.start()]
    return prefix + _MISSION_LOCATIONS.get(location, map_name(location)) + suffix


def mission_name(name: str) -> str:
    if language() != "en":
        return name
    title, *tags = name.split(" · ")
    mode, separator, location = title.removeprefix("【").partition("】")
    if separator and title.startswith("【"):
        mode = _MISSION_TYPE.sub(lambda match: _MISSION_TYPES.get(match[0], match[0]), mode)
        number, _, location = location.lstrip().partition(" ") if location.startswith("#") else ("", "", location)
        location = _mission_location(location.strip())
        title = " ".join(piece for piece in (f"[{mode}]", number, location) if piece)
    else:
        title = _MISSION_TYPE.sub(lambda match: _MISSION_TYPES.get(match[0], match[0]), map_name(title))
    return " · ".join([title, *(tr(tag) if not (match := _LAYOUT_TAG.fullmatch(tag))
                                 else f"Layout {match[1]}" for tag in tags)])
