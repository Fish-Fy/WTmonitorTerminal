"""标点/路径/方案 数据模型。

坐标系: 统一使用 (cx, cy) 的"场景归一化坐标"
  cx = pixel_x / image_width  ∈ [0, 1]
  cy = pixel_y / image_height ∈ [0, 1]
这样跨分辨率/缩放/底图尺寸变化都能稳定复现。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..i18n import tr
from .areas import Area, MAX_AREAS


# 标点类型预设: key -> (显示名, 颜色hex, 形状: 仅供旧数据兼容; 图标见 ui/marker_icons.py 符号表)
MARKER_TYPES: dict[str, tuple[str, str, str]] = {
    "sniper":   ("狙击点",     "#ff3b30", "cross"),
    "engage":   ("交战点",     "#ff9500", "diamond"),
    "intense":  ("交战点", "#af2b1e", "star"),
    "scout":    ("侦察点",     "#34c759", "tri"),
    "rally":    ("出生点",     "#007aff", "square"),
    "walk":     ("路线点",     "#00e5ff", "dot"),      # 连续画路线用(用户 2026-10-12)
    "custom":   ("自定义",     "#8e8e93", "dot"),
    # 下面四种只供旧标点显示，保留定义 —— 否则旧方案里
    # 用过它们的标点会退化成"自定义"灰点, 颜色与图标全丢。见 HIDDEN_TYPES。
    "ambush":   ("伏击点",     "#5856d6", "dot"),
    "artillery":("炮击点",     "#ff2d55", "tri"),
    "flank":    ("迂回点",     "#30d5c8", "diamond"),
    "supply":   ("补给点",     "#ffd60a", "square"),
}

# 保留旧图标，但添加和编辑都使用同一套可选类型。
HIDDEN_TYPES: frozenset[str] = frozenset({"intense", "ambush", "artillery", "flank", "supply"})
MARKER_TYPE_CHOICES = tuple((key, name) for key, (name, _, _) in MARKER_TYPES.items()
                            if key not in HIDDEN_TYPES)


@dataclass
class Marker:
    id: str
    type: str = "custom"
    cx: float = 0.5
    cy: float = 0.5
    label: str = ""
    note: str = ""

    @classmethod
    def new(cls, type_: str, cx: float, cy: float, label: str = "") -> "Marker":
        return cls(id=uuid.uuid4().hex[:12], type=type_, cx=cx, cy=cy, label=label)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PathLink:
    """两点连线。from_id/to_id 为 Marker.id; enabled=True 时绘制。"""
    from_id: str
    to_id: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scheme:
    """一张地图的一套标点方案。同一地图可有多套。"""
    id: str
    scene: str
    name: str = "未命名方案"
    markers: list[Marker] = field(default_factory=list)
    paths: list[PathLink] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    areas: list[Area] = field(default_factory=list)

    @classmethod
    def fresh(cls, scene: str, name: str = "未命名方案") -> "Scheme":
        return cls(id=uuid.uuid4().hex[:12], scene=scene, name=name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scene": self.scene,
            "name": self.name,
            "markers": [m.to_dict() for m in self.markers],
            "paths": [p.to_dict() for p in self.paths],
            **({"task_ids": self.task_ids} if self.task_ids else {}),
            **({"areas": [area.to_dict() for area in self.areas]} if self.areas else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scheme":
        markers = [Marker(**m) for m in d.get("markers", [])]
        paths = [PathLink(**p) for p in d.get("paths", [])]
        area_records = d.get("areas", [])
        if not isinstance(area_records, list) or len(area_records) > MAX_AREAS:
            raise ValueError(tr('每套方案最多保存 {0} 个区域', MAX_AREAS))
        areas = [Area.from_dict(record) for record in area_records]
        if len({area.id for area in areas}) != len(areas):
            raise ValueError(tr("区域标识重复"))
        task_ids = d.get("task_ids")
        if not isinstance(task_ids, list):
            task_ids = [d.get("task_id")]
        return cls(
            id=d.get("id", uuid.uuid4().hex[:12]),
            scene=d.get("scene", "_unknown"),
            name=d.get("name", "未命名方案"),
            markers=markers,
            paths=paths,
            task_ids=list(dict.fromkeys(key for key in task_ids if isinstance(key, str) and key)),
            areas=areas,
        )
