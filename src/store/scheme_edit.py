"""Copy and translate annotations while preserving internal route links."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from ..i18n import tr
from .areas import Area, MAX_AREAS
from .models import Scheme


def selection(scheme, markers, areas):
    return Scheme(scheme.id, scheme.scene, markers=[deepcopy(m) for m in scheme.markers if m.id in markers],
                  paths=[deepcopy(p) for p in scheme.paths if p.from_id in markers and p.to_id in markers],
                  areas=[a for a in scheme.areas if a.id in areas])


def transfer(target, group, position=None, *, move=False):
    """Validate the whole result before changing the target."""
    if target.scene != group.scene:
        raise ValueError(tr("请选择同一地图上的方案"))
    if not move and len(target.areas) + len(group.areas) > MAX_AREAS:
        raise ValueError(tr("每套方案最多保存 {0} 个区域", MAX_AREAS))
    points = [(m.cx, m.cy) for m in group.markers]
    for a in group.areas:
        points.extend([a.geometry[:2]] if a.shape == "circle" else a.geometry)
    if not points:
        return set(), set()
    dx = dy = 0
    if position is not None:
        xs, ys = zip(*points)
        dx = position[0] - (min(xs) + max(xs)) / 2
        dy = position[1] - (min(ys) + max(ys)) / 2
    ids = {m.id: m.id if move else uuid4().hex[:12] for m in group.markers}
    markers = [replace(m, id=ids[m.id], cx=m.cx + dx, cy=m.cy + dy) for m in group.markers]
    if position and any(not (0 <= m.cx <= 1 and 0 <= m.cy <= 1) for m in markers):
        raise ValueError(tr("部分标点将超出地图，请选择更靠内的位置"))
    areas = []
    for a in group.areas:
        if a.shape == "circle":
            x, y, radius = a.geometry
            geometry = (x + dx, y + dy, radius)
        else:
            geometry = [(x + dx, y + dy) for x, y in a.geometry]
        areas.append(Area.new(a.type, a.shape, geometry, area_id=a.id if move else None))
    if move:
        moved_markers = {m.id: m for m in markers}
        moved_areas = {a.id: a for a in areas}
        target.markers = [moved_markers.get(m.id, m) for m in target.markers]
        target.areas = [moved_areas.get(a.id, a) for a in target.areas]
    else:
        target.markers.extend(markers)
        target.areas.extend(areas)
        target.paths.extend(replace(p, from_id=ids[p.from_id], to_id=ids[p.to_id]) for p in group.paths
                            if p.from_id in ids and p.to_id in ids)
    return set(ids.values()), {a.id for a in areas}
