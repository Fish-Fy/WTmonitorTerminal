"""遥测解析与姿态平滑（纯逻辑，不依赖 Qt，方便单测）。

符号标定（真机互证；夹具 tests/fixtures/{indicators,state}_{climb,dive}_real.json）：
    爬升 Vy = +64.4 m/s  →  aviahorizon_pitch = -6.26°
    俯冲 Vy = -205  m/s  →  aviahorizon_pitch = +50.24°
  ⇒ 这个字段与航空惯例（抬头为正）**相反**，显示前必须取负。
  滚转同理：实测右转弯（罗盘 99°→125°）时 aviahorizon_roll 为负 ⇒ 也要取负。
翻转开关在 config.yaml 的 attitude.invert_pitch / invert_roll —— 若你飞出来觉得方向反了，改配置即可，
不必动代码。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any


def _f(d: dict | None, *keys: str) -> float | None:
    """取第一个存在的数值键（战雷键名带单位后缀，逐个试）。"""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
    return None


@dataclass(frozen=True)
class Attitude:
    """/indicators 里的姿态相关量（已按标定翻正符号）。"""
    pitch: float = 0.0          # 度，抬头为正
    roll: float = 0.0           # 度，右倾(右翼下)为正
    heading: float = 0.0        # 度，罗盘 0..360
    vario: float = 0.0          # m/s
    aoa: float | None = None    # 度（indicators 自带的 aoa）
    speed: float | None = None  # indicators 的 speed（与 /state 的空速口径不同，仅作参考）
    vehicle: str = ""           # 机型内部名，如 f_15e
    valid: bool = True


@dataclass(frozen=True)
class StateSample:
    """/state 的关键量（键名带单位后缀，缺失一律 None，绝不猜 0）。"""
    valid: bool = False
    altitude_m: float | None = None
    tas_kmh: float | None = None
    ias_kmh: float | None = None
    mach: float | None = None
    aoa_deg: float | None = None
    aos_deg: float | None = None
    vy_ms: float | None = None
    ny: float | None = None
    roll_rate_degs: float | None = None
    fuel_kg: float | None = None
    fuel0_kg: float | None = None
    throttle_pct: float | None = None
    gear_pct: float | None = None
    flaps_pct: float | None = None


def parse_indicators(d: dict | None, invert_pitch: bool = True, invert_roll: bool = True) -> Attitude | None:
    """d 为 /indicators 的 JSON dict；不可用返回 None（姿态仪转"无信号"，不画假读数）。"""
    if not isinstance(d, dict) or not d.get("valid"):
        return None
    p = _f(d, "aviahorizon_pitch")
    r = _f(d, "aviahorizon_roll")
    if p is None or r is None:
        return None                     # 陆战/无仪表时这些键不存在
    h = _f(d, "compass", "compass1") or 0.0
    return Attitude(
        pitch=(-p if invert_pitch else p),
        roll=(-r if invert_roll else r),
        heading=h % 360.0,
        vario=_f(d, "vario") or 0.0,
        aoa=_f(d, "aoa"),
        speed=_f(d, "speed"),
        vehicle=str(d.get("type", "") or ""),
        valid=True,
    )


def parse_state(d: dict | None) -> StateSample:
    """d 为 /state 的 JSON dict；缺键得 None。"""
    if not isinstance(d, dict):
        return StateSample()
    return StateSample(
        valid=bool(d.get("valid", False)),
        altitude_m=_f(d, "H, m", "AltitudeH", "Altitude"),
        tas_kmh=_f(d, "TAS, km/h", "TAS"),
        ias_kmh=_f(d, "IAS, km/h", "IAS"),
        mach=_f(d, "M", "MNo", "Mach"),
        aoa_deg=_f(d, "AoA, deg", "AoA"),
        aos_deg=_f(d, "AoS, deg", "AoS"),
        vy_ms=_f(d, "Vy, m/s", "Vy"),
        ny=_f(d, "Ny", "GForce"),
        roll_rate_degs=_f(d, "Wx, deg/s"),
        fuel_kg=_f(d, "Mfuel, kg", "Fuel"),
        fuel0_kg=_f(d, "Mfuel0, kg"),
        throttle_pct=_f(d, "throttle 1, %", "throttle 2, %"),
        gear_pct=_f(d, "gear, %"),
        flaps_pct=_f(d, "flaps, %"),
    )


# ------------------------------------------------------------------ 平滑(纯函数)
def approach(cur: float, target: float, dt: float, tau: float) -> float:
    """一阶低通: tau 越小越跟手。target None 时返回 cur。"""
    if target is None:
        return cur
    if tau <= 0 or dt <= 0:
        return float(target)
    a = 1.0 - math.exp(-dt / tau)
    return cur + (float(target) - cur) * a


def approach_angle(cur: float, target: float | None, dt: float, tau: float) -> float | None:
    """按**最短弧**插值角度（罗盘 358°→2° 必须走 0° 近路，不能倒绕 356°）。"""
    if target is None:
        return None
    delta = ((float(target) - cur + 180.0) % 360.0) - 180.0
    return (cur + delta * (1.0 if tau <= 0 or dt <= 0 else 1.0 - math.exp(-dt / tau))) % 360.0


def monotonic() -> float:
    return time.monotonic()
