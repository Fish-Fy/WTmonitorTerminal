"""Labels shared by the initial phone page and its live updates."""

from html import escape
import json
import re

from ..i18n import normalize_language


_TEXT = {
    "appName": ("战术终端", "WT Tactical Terminal"),
    "pageTitle": ("战术终端 · 手机显示", "WT Tactical Terminal · Phone view"),
    "map": ("战术地图", "Tactical map"),
    "planning": ("离线规划", "Offline planning"),
    "connecting": ("连接中", "Connecting"),
    "mapTools": ("地图工具", "Map tools"),
    "mapControls": ("地图操作", "Map controls"),
    "zoomOut": ("缩小地图", "Zoom out"),
    "zoomIn": ("放大地图", "Zoom in"),
    "reset": ("复位", "Reset"),
    "focus": ("地图铺满", "Map only"),
    "leaveFocus": ("退出铺满", "Show roster"),
    "fullscreen": ("全屏", "Full screen"),
    "exitFullscreen": ("退出全屏", "Exit full screen"),
    "landscape": ("横屏显示", "Landscape"),
    "exitLandscape": ("退出横屏", "Exit landscape"),
    "fullscreenUnavailable": ("此浏览器不支持网页全屏。iPhone/iPad 可在 Safari 分享菜单中选择“添加到主屏幕”，从主屏幕打开。",
                              "This browser cannot enter full screen. On iPhone or iPad, use Add to Home Screen in Safari and open it from there."),
    "fullscreenRejected": ("浏览器未允许全屏，请使用系统浏览器重新打开后再试。",
                           "Full screen was blocked. Open this page in your device's browser and try again."),
    "liveMap": ("实时战术地图", "Live tactical map"),
    "mapAlt": ("电脑当前战术地图，含标点和地图叠加项", "Current tactical map with markers and overlays"),
    "waitingMap": ("等待电脑地图", "Waiting for desktop map"),
    "waitingHint": ("电脑程序保持运行，进入对局后会自动同步。", "Keep the terminal running on your PC. The map syncs when you join a battle."),
    "waitingFrame": ("等待画面", "Waiting for frame"),
    "roster": ("本局载具识别", "Battle Roster"),
    "emptyCount": ("0 人", "Players: 0"),
    "playerCount": ("{0} 人", "Players: {0}"),
    "search": ("搜索昵称或载具", "Search nicknames or vehicles"),
    "hideLeft": ("隐藏已离场", "Hide players who left"),
    "highlight": ("高亮击毁过我", "Highlight players who killed me"),
    "nickname": ("昵称", "Nickname"),
    "vehicle": ("载具", "Vehicle"),
    "kills": ("击杀", "Kills"),
    "status": ("状态", "Status"),
    "unknownStatus": ("状态未知", "Status unknown"),
    "killedMe": ("本局击毁过你 {0} 次", "Times they killed you this battle: {0}"),
    "killCount": ("击杀 {0}", "Kills: {0}"),
    "enemy": ("敌方", "Enemies"),
    "friend": ("友方", "Allies"),
    "unknown": ("未定阵营", "Unknown side"),
    "hiddenCount": ("已隐藏 {0} 人（离场）", "Hidden (left): {0}"),
    "noMatch": ("没有匹配的昵称或载具", "No matching nicknames or vehicles"),
    "everyoneLeft": ("全部玩家已离场", "All players have left"),
    "waitingRoster": ("等待本局载具识别数据", "Waiting for battle roster data"),
    "connected": ("已连接", "Connected"),
    "reconnecting": ("重连中", "Reconnecting"),
    "disconnected": ("连接中断 · 正在重连", "Disconnected · Reconnecting"),
    "retrying": ("画面加载中断 · 正在重试", "Frame interrupted · Retrying"),
    "delayed": ("更新延迟", "Update delayed"),
    "cached": ("缓存画面", "Cached frame"),
    "live": ("实时", "Live"),
    "delayedHold": ("画面更新延迟 · 保留上一画面", "Frame delayed · Showing the last frame"),
}
_TOKEN = re.compile(r"\{\{([a-zA-Z]+)\}\}")


def render_copy(template: str, locale: str) -> str:
    locale = normalize_language(locale)
    labels = {key: english if locale == "en" else chinese
              for key, (chinese, english) in _TEXT.items()}
    payload = json.dumps(labels, ensure_ascii=False).replace("<", "\\u003c")
    return _TOKEN.sub(lambda match: escape(labels[match[1]]), template).replace(
        "<!--LANG-->", locale).replace("<!--COPY-->", payload)
