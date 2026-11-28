"""战术深色 HUD 主题(用户 2026-10-07 定稿): 全局唯一色板, 桌面 QSS 与 Web CSS 共用令牌。

范围声明: 这里只管**外壳**(窗口/面板/按钮/提示/悬浮窗行)。
地图内部绘制色(占领区红蓝白菱形、敌红/友蓝单位色、玩家箭头)是更早定稿的用户红线, 不动。
Web 端经 css_variables() 拿到同一份令牌, 杜绝两边色板漂移。
"""
from __future__ import annotations

# ---- 色板(唯一来源) ----
BG = "#0f1419"        # 主底(近黑藏蓝)
PANEL = "#161d24"     # 面板底
RAISED = "#1d2630"    # 浮起控件底(按钮/输入框)
BORDER = "#26313b"    # 细线框
TEXT = "#d5dbe1"      # 正文
DIM = "#8b95a1"       # 弱字/提示
ACCENT = "#ffb300"    # 主强调(琥珀): 标题箭头/焦点/告警橙hint
OK = "#35d0ba"        # 安全(青绿): 能连上/正常/hint 绿
DANGER = "#ff5c5c"    # 危险: 我挨打/断开
FRIEND = "#4da3ff"    # 我动手/友方强调
MONO = "'Cascadia Mono','JetBrains Mono','Consolas',monospace"
ROSTER_STATUS_COLORS = {"alive": OK, "killed": ACCENT, "left": "#a89abb", "unknown": DIM}

# 兼容历史命名的语义别名(收编旧内联样式时对照用)
HINT_WARN = ACCENT    # 旧 #af5200 hint 橙
HINT_OK = OK          # 旧 #34c759 hint 绿
HINT_DIM = DIM        # 旧 #8e8e93 hint 灰


def tokens() -> dict[str, str]:
    """全部令牌(测试断言完整性/格式用, Web 端经 css_variables 消费)。"""
    return {"bg": BG, "panel": PANEL, "raised": RAISED, "border": BORDER, "text": TEXT,
            "dim": DIM, "accent": ACCENT, "ok": OK, "danger": DANGER, "friend": FRIEND,
            "mono": MONO}


def css_variables() -> str:
    """:root{...} 形式的 CSS 变量块, 由 Web 服务端注入页面 <head>。"""
    t = {**tokens(), **{f"roster-{status}": color for status, color in ROSTER_STATUS_COLORS.items()}}
    pairs = "; ".join(f"--{k}: {v}" for k, v in t.items())
    return f":root {{ {pairs}; }}"


def app_qss() -> str:
    """桌面端全局样式表(main.py 装载)。只动外壳, 不干预地图场景内绘制。"""
    return f"""
QMainWindow, QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif;
    font-size: 13px;
}}
QGraphicsView {{
    background: {BG};
    border: none;
}}
QWidget#windowTitleBar, QWidget#windowTitleBar QWidget {{ background: {PANEL}; }}
QWidget#windowTitleBar {{ border-bottom: 1px solid {BORDER}; }}
QLabel#windowTitleText {{ color: {ACCENT}; font-weight: bold; background: transparent; }}
QWidget#windowResizeEdge {{ background: transparent; border: none; }}
QToolTip {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}
/* 折叠面板标题( CollapsibleBox 的 QToolButton ) */
QToolButton {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    font-weight: bold;
}}
QToolButton:hover {{ border-color: {ACCENT}; }}
/* 树/列表(载具识别、标点列表) */
QTreeWidget, QListWidget, QListView, QTreeView {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    alternate-background-color: {RAISED};
    outline: none;
}}
QTreeWidget::item, QListWidget::item {{ padding: 1px 2px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {RAISED};
    color: {ACCENT};
}}
QWidget#schemeRow {{ background: {PANEL}; }}
QWidget#schemeRow[active="true"] {{ background: {RAISED}; }}
QLabel#schemeRowName, QWidget#schemeRow QCheckBox {{ background: transparent; }}
QLabel#schemeRowName[active="true"] {{ color: {ACCENT}; }}
QHeaderView::section {{
    background: {BG};
    color: {DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 3px 4px;
}}
/* 按钮与输入 */
QPushButton {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 12px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:checked {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton[primary="true"] {{ color: {ACCENT}; }}
QPushButton[destructive="true"]:hover {{ border-color: {DANGER}; color: {DANGER}; }}
QPushButton:disabled, QComboBox:disabled {{ color: {DIM}; background: {PANEL}; border-color: {BORDER}; }}
QPushButton[sidebarNav="true"] {{ padding: 4px 5px; }}
QPushButton[sidebarNav="true"]:checked {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton[schemeAction="true"] {{ padding: 4px 8px; }}
QTabBar#workspaceTabs::tab {{
    background: {PANEL};
    color: {DIM};
    border-bottom: 2px solid {BORDER};
    padding: 4px 8px;
}}
QTabBar#workspaceTabs::tab:selected {{ border-color: {ACCENT}; color: {ACCENT}; }}
QTabBar#workspaceTabs::tab:hover {{ color: {TEXT}; }}
QLabel[workspaceCount="true"] {{ background: transparent; color: {DIM}; font-size: 11px; }}
QWidget#sidebarContext {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; }}
QWidget#sidebarContext QLabel {{ background: transparent; border: none; }}
QLabel#mapTitle, QLabel[pageTitle="true"] {{ font-size: 15px; font-weight: bold; }}
QLabel[muted="true"], QLabel#contextHint {{ color: {DIM}; font-size: 12px; }}
QLabel#contextBadge {{ color: {DIM}; padding: 2px 0; font-size: 12px; }}
QLabel#contextBadge[tone="live"] {{ color: {OK}; }}
QLabel#contextBadge[tone="planning"] {{ color: {ACCENT}; }}
QScrollArea {{ border: none; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT}; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 6px;
    selection-background-color: {ACCENT};
    selection-color: {BG};
}}
QLineEdit:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {RAISED};
    selection-color: {ACCENT};
}}
QCheckBox {{ spacing: 6px; }}
/* 菜单(地图右键) */
QMenu {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{ padding: 4px 24px 4px 16px; border-radius: 3px; }}
QMenu::item:selected {{ background: {RAISED}; color: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 2px; }}
/* 滚动条: 窄、暗、不抢戏 */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {DIM}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {BORDER}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
/* 状态栏/分割条 */
QStatusBar {{ background: {BG}; color: {DIM}; border-top: 1px solid {BORDER}; }}
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 5px; }}
QSplitter::handle:hover {{ background: {BORDER}; }}
/* 对话框/数字框箭头 */
QMessageBox QLabel {{ color: {TEXT}; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
"""
