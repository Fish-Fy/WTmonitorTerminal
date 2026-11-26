"""配置加载与应用常量。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .i18n import tr


def save_game_nickname(path: Path | str, nickname: str) -> None:
    """只修改昵称字段；其他配置保留，写入失败时原文件不变。"""
    import os
    import tempfile
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or not isinstance(raw.get("identity", {}), dict):
        raise ValueError(tr("配置文件格式异常，请先检查 config.yaml"))
    raw.setdefault("identity", {})["my_nick"] = nickname
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
        os.replace(temporary, p)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass
class PollCfg:
    state: int = 5          # 服务端串行, 官方前端整体 2Hz; 别再往上加
    map_img: int = 2        # 核对 map_generation 的节奏(底图按 gen 变化才下载)
    mission: int = 1
    mission_idle: float = 0.2
    indicators: float = 8   # /indicators: 姿态仪数据源(仅 ~1.1KB), 平滑交给插值, 不必更高


@dataclass
class SessionCfg:
    state_timeout: float = 5.0


@dataclass
class ReplayCfg:
    enabled: bool = False


@dataclass
class IdentityCfg:
    """我自己是谁 —— 8111 不提供阵营与"哪个是我", 载具识别靠这个昵称从击杀文案反推敌友。
    匹配忽略大小写/战队前缀/零宽分隔符, 所以填 examplepilot 也能命中 ^DEMO^ ExamplePilot。"""
    my_nick: str = ""


@dataclass
class AttitudeCfg:
    """姿态仪与叠加层。invert_* 是真机标定结果：
    爬升(Vy +64.4)时 aviahorizon_pitch = -6.26；俯冲(Vy -205)时 +50.24 ⇒ 抬头为负, 必须翻正。
    若飞出来觉得方向不对, 改这里就行, 不用动代码。"""
    invert_pitch: bool = True
    invert_roll: bool = True
    size: int = 148
    smooth_tau: float = 0.08
    stale_after: float = 1.5
    show: tuple[str, ...] = ("attitude",)      # 默认只开姿态仪, 其余在「载具状态显示」里勾
    # 兼容旧配置字段。2026-10-25 起仪表固定仅飞机显示，此字段不再取消限制。
    auto_by_vehicle: bool = True


@dataclass
class AlertCfg:
    """状态栏提示阈值 + 威胁告警参数。

    声音口径(2026-09-29 用户修订红线⑤): 低油/临界迎角/我被击落**仍然只有文字、不发声**;
    只有威胁告警(附近队友连续阵亡 / 同一人连续击杀绕后)允许播 sound_file 指向的自定义
    音频(推荐 WAV), 留空 = 静音。发声通道集中在 src/ui/sfx.py(测试钉住)。"""
    enabled: bool = True
    low_fuel_kg: float = 1500.0
    stall_aoa_deg: float = 18.0
    aoa_hysteresis: float = 2.0
    # ---- 威胁告警(见 src/agent/threatwatch.py) ----
    sound_file: str = ""          # 自定义告警音频(WAV); 空 = 静音
    nearby_radius: float = 0.10   # "附近"队友判定半径(归一化; 0.10 ≈ 地图宽度 10%)
    nearby_count: int = 2         # 窗口内附近阵亡达到几个 → 告警
    nearby_window: float = 8.0    # 累计窗口(秒)
    rampage_gap: float = 20.0     # 连续击杀: 相邻两杀最大间隔(秒)
    rampage_count: int = 3        # 连续击杀: 从第几杀开始逐次告警


@dataclass
class CombatOverlayCfg:
    """独立置顶透明小窗(交战车型速览 + 告警横幅)。x/y < 0 = 自动贴屏幕右缘。"""
    enabled: bool = True
    x: int = -1
    y: int = -1
    ttl: float = 8.0              # 交战行存活秒数
    max_rows: int = 6             # 行数封顶, 防刷屏


@dataclass
class WebCfg:
    """手机/平板只读镜像(2026-10-07 定稿)。**默认关闭**; 无鉴权, 只发只读快照/事件流,
    要开只绑家庭局域网。host="0.0.0.0" 才能被手机访问到(注意防火墙放行)。"""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8321


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8111
    config_blk_path: str = ""
    poll: PollCfg = field(default_factory=PollCfg)
    session: SessionCfg = field(default_factory=SessionCfg)
    replay: ReplayCfg = field(default_factory=ReplayCfg)
    identity: IdentityCfg = field(default_factory=IdentityCfg)
    alerts: AlertCfg = field(default_factory=AlertCfg)
    attitude: AttitudeCfg = field(default_factory=AttitudeCfg)
    combat_overlay: CombatOverlayCfg = field(default_factory=CombatOverlayCfg)
    web: WebCfg = field(default_factory=WebCfg)
    data_dir: str = "data"


def _section(d: dict[str, Any], key: str) -> dict[str, Any]:
    return d.get(key, {}) or {}


def _num(d: dict[str, Any], key: str, default, cast):
    """容错取数: 缺失或类型不对都回默认, 不让一份手改坏的 yaml 把程序带崩。"""
    try:
        return cast(d[key])
    except (KeyError, TypeError, ValueError):
        return cast(default)


def _names(d: dict[str, Any], key: str, default):
    raw = d.get(key, default)
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return tuple(default)


def load_config(path: Path | str) -> Config:
    raw: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    po = _section(raw, "poll")
    se = _section(raw, "session")
    rp = _section(raw, "replay")
    idn = _section(raw, "identity")
    al = _section(raw, "alerts")
    at = _section(raw, "attitude")
    co = _section(raw, "combat_overlay")
    web = _section(raw, "web")
    dft = Config()
    return Config(
        host=str(raw.get("host", dft.host)),
        port=_num(raw, "port", dft.port, int),
        config_blk_path=str(raw.get("config_blk_path", "") or ""),
        poll=PollCfg(
            state=_num(po, "state", dft.poll.state, int),
            map_img=_num(po, "map_img", dft.poll.map_img, int),
            mission=_num(po, "mission", dft.poll.mission, int),
            mission_idle=_num(po, "mission_idle", dft.poll.mission_idle, float),
            indicators=_num(po, "indicators", dft.poll.indicators, float),
        ),
        session=SessionCfg(state_timeout=_num(se, "state_timeout", dft.session.state_timeout, float)),
        replay=ReplayCfg(enabled=bool(rp.get("enabled", dft.replay.enabled))),
        identity=IdentityCfg(my_nick=str(idn.get("my_nick", "") or "").strip()),
        alerts=AlertCfg(
            enabled=bool(al.get("enabled", dft.alerts.enabled)),
            low_fuel_kg=_num(al, "low_fuel_kg", dft.alerts.low_fuel_kg, float),
            stall_aoa_deg=_num(al, "stall_aoa_deg", dft.alerts.stall_aoa_deg, float),
            aoa_hysteresis=_num(al, "aoa_hysteresis", dft.alerts.aoa_hysteresis, float),
            sound_file=str(al.get("sound_file", dft.alerts.sound_file) or ""),
            nearby_radius=_num(al, "nearby_radius", dft.alerts.nearby_radius, float),
            nearby_count=_num(al, "nearby_count", dft.alerts.nearby_count, int),
            nearby_window=_num(al, "nearby_window", dft.alerts.nearby_window, float),
            rampage_gap=_num(al, "rampage_gap", dft.alerts.rampage_gap, float),
            rampage_count=_num(al, "rampage_count", dft.alerts.rampage_count, int),
        ),
        combat_overlay=CombatOverlayCfg(
            enabled=bool(co.get("enabled", dft.combat_overlay.enabled)),
            x=_num(co, "x", dft.combat_overlay.x, int),
            y=_num(co, "y", dft.combat_overlay.y, int),
            ttl=_num(co, "ttl", dft.combat_overlay.ttl, float),
            max_rows=_num(co, "max_rows", dft.combat_overlay.max_rows, int),
        ),
        attitude=AttitudeCfg(
            invert_pitch=bool(at.get("invert_pitch", dft.attitude.invert_pitch)),
            invert_roll=bool(at.get("invert_roll", dft.attitude.invert_roll)),
            size=_num(at, "size", dft.attitude.size, int),
            smooth_tau=_num(at, "smooth_tau", dft.attitude.smooth_tau, float),
            stale_after=_num(at, "stale_after", dft.attitude.stale_after, float),
            show=_names(at, "show", dft.attitude.show),
        ),
        data_dir=str(raw.get("data_dir", dft.data_dir) or dft.data_dir),
        web=WebCfg(enabled=bool(web.get("enabled", dft.web.enabled)),
                   host=str(web.get("host", dft.web.host) or dft.web.host),
                   port=max(0, min(65535, _num(web, "port", dft.web.port, int)))),
    )
