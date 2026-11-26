"""WT3/WT4 binary payloads. Type IDs and bit layouts are permanent wire values."""
import struct

from ..i18n import tr
from .areas import MAX_AREAS, MAX_VERTICES, PRECISION


# Append new types here; never reorder existing IDs.
TYPES = ("sniper", "engage", "intense", "scout", "rally", "walk", "custom",
         "ambush", "artillery", "flank", "supply")
HEADER = struct.Struct("<HHB")
WORD = struct.Struct("<I")
COORD = struct.Struct("<H")
EXTENDED_COORD = struct.Struct("<i")
EXTENDED_CIRCLE = 1  # Outside the WT3 polygon vertex-count range.


def circle_fits_wt3(geometry):
    return all(0 <= value <= PRECISION for value in geometry)


def pack(points, links, areas):
    data = bytearray(HEADER.pack(len(points), len(links), len(areas)))
    for kind, x, y in points:
        data.extend(WORD.pack(TYPES.index(kind) | (x << 4) | (y << 18)))
    for source, target, enabled in links:
        data.extend(WORD.pack(source | (target << 13) | (int(enabled) << 26)))
    for kind, shape, geometry in areas:
        data.append(TYPES.index(kind))
        if shape == "circle":
            tag = 0 if circle_fits_wt3(geometry) else EXTENDED_CIRCLE
            data.append(tag)
            coord = EXTENDED_COORD if tag == EXTENDED_CIRCLE else COORD
            values = geometry
        else:
            data.append(len(geometry))
            coord = COORD
            values = [value for point in geometry for value in point]
        for value in values:
            data.extend(coord.pack(value))
    return bytes(data)


def unpack(data, max_points, max_links, *, allow_extended=False):
    if len(data) < HEADER.size:
        raise ValueError(tr("分享码内容不完整"))
    point_count, link_count, area_count = HEADER.unpack_from(data)
    if (point_count > max_points or link_count > max_links or area_count > MAX_AREAS
            or not point_count + area_count):
        raise ValueError(tr("分享码中的图元数量无效"))
    offset = HEADER.size

    def read(format_):
        nonlocal offset
        if offset + format_.size > len(data):
            raise ValueError(tr("分享码内容不完整"))
        value, = format_.unpack_from(data, offset)
        offset += format_.size
        return value

    def type_name(index):
        if index >= len(TYPES):
            raise ValueError(tr("分享码中的类型无效"))
        return TYPES[index]

    points = []
    for _ in range(point_count):
        word = read(WORD)
        points.append([type_name(word & 15), (word >> 4) & 16383, word >> 18])
    links = []
    for _ in range(link_count):
        word = read(WORD)
        if word >> 27:
            raise ValueError(tr("分享码中的连线无效"))
        links.append([word & 8191, (word >> 13) & 8191, bool(word & (1 << 26))])
    areas = []
    byte = struct.Struct("<B")
    for _ in range(area_count):
        kind = type_name(read(byte))
        count = read(byte)
        if count == 0:
            shape = "circle"
            geometry = [read(COORD) for _ in range(3)]
            if not circle_fits_wt3(geometry):
                raise ValueError(tr("分享码中的圆形坐标或半径超出范围"))
        elif allow_extended and count == EXTENDED_CIRCLE:
            shape = "circle"
            geometry = [read(EXTENDED_COORD) for _ in range(3)]
        elif 3 <= count <= MAX_VERTICES:
            shape = "polygon"
            geometry = [[read(COORD), read(COORD)] for _ in range(count)]
        else:
            raise ValueError(tr("分享码中的区域顶点数量无效"))
        areas.append([kind, shape, geometry])
    if offset != len(data):
        raise ValueError(tr("分享码含有多余内容"))
    return [points, links, areas] if areas else [points, links]
