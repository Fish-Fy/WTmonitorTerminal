"""方案库的本地读写: data/schemes/<scene>.json 内含该地图所有方案列表。"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from .models import Scheme
from ..agent.mapimg import atomic_write, synchronized, safe_scene_name


log = logging.getLogger(__name__)


def _scene_safe(scene: str | None) -> str:
    return safe_scene_name(scene)


class SchemeDataError(ValueError):
    """现有方案无法完整读取时，禁止把它当成空库覆盖。"""


def decode_schemes(raw) -> list[Scheme]:
    arr = raw if isinstance(raw, list) else raw.get("schemes") if isinstance(raw, dict) else None
    if not isinstance(arr, list) or not all(isinstance(item, dict) for item in arr):
        raise SchemeDataError("方案文件必须包含方案列表")
    result = []
    ids = set()
    try:
        for record in arr:
            scheme = Scheme.from_dict(record)
            if not isinstance(scheme.id, str) or not scheme.id or scheme.id in ids:
                raise ValueError("方案标识缺失或重复")
            ids.add(scheme.id)
            marker_ids = set()
            for marker in scheme.markers:
                if not isinstance(marker.id, str) or not marker.id or marker.id in marker_ids:
                    raise ValueError("标点标识缺失或重复")
                marker_ids.add(marker.id)
                if not all(math.isfinite(float(v)) for v in (marker.cx, marker.cy)):
                    raise ValueError("标点坐标无效")
                marker.cx, marker.cy = float(marker.cx), float(marker.cy)
            result.append(scheme)
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise SchemeDataError(f"方案数据不完整：{exc}") from exc
    return result


def read_schemes(path: Path) -> list[Scheme]:
    try:
        return decode_schemes(json.loads(path.read_text(encoding="utf-8-sig")))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SchemeDataError(f"方案文件损坏，已保留原文件：{path.name}") from exc


def scheme_file(data_root: Path, scene: str | None) -> Path:
    d = data_root / "schemes"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_scene_safe(scene)}.json"


@synchronized
def load_schemes(data_root: Path, scene: str | None) -> list[Scheme]:
    f = scheme_file(data_root, scene)
    if not f.exists():
        return []
    try:
        return read_schemes(f)
    except Exception:
        log.exception("读取方案失败: %s", f)
        return []


@synchronized
def save_schemes(data_root: Path, scene: str | None, schemes: list[Scheme]) -> Path:
    f = scheme_file(data_root, scene)
    if f.exists():
        read_schemes(f)                       # 验证失败时拒绝覆盖，原文件可继续修复
    payload: list[dict[str, Any]] = [s.to_dict() for s in schemes]
    decode_schemes(payload)
    atomic_write(f, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return f
