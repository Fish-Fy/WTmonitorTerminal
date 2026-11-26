"""Versioned, bounded offline sharing of tactical scheme geometry."""
import base64
import binascii
import json
import math
import zlib

from ..i18n import tr
from .models import MARKER_TYPES, Marker, PathLink, Scheme
from .areas import Area, MAX_AREAS
from . import share_binary, share_text

PREFIX = "WT1."
AREA_PREFIX = "WT2."
COMPACT_PREFIX = "WT3."
EXTENDED_PREFIX = "WT4."
MAX_CODE = 262144
MAX_RAW = 1048576
MAX_POINTS = 5000
MAX_LINKS = 10000


def _validate(payload, *, with_areas):
    if not isinstance(payload, list) or len(payload) != (3 if with_areas else 2):
        raise ValueError(tr("分享码内容格式不正确"))
    points, links = payload[:2]
    if not isinstance(points, list) or not (0 if with_areas else 1) <= len(points) <= MAX_POINTS:
        raise ValueError(tr("标点数量无效，最多支持 5000 个标点"))
    if not isinstance(links, list) or len(links) > MAX_LINKS:
        raise ValueError(tr("连线数量超出限制"))
    for point in points:
        if (not isinstance(point, list) or len(point) != 3
                or not isinstance(point[0], str) or point[0] not in MARKER_TYPES
                or any(type(value) is not int or not 0 <= value <= 10000 for value in point[1:])):
            raise ValueError(tr("分享码中的标点类型或坐标无效"))
    for link in links:
        if (not isinstance(link, list) or len(link) != 3 or type(link[2]) is not bool
                or any(type(value) is not int or not 0 <= value < len(points) for value in link[:2])):
            raise ValueError(tr("分享码中的连线无效"))
    if with_areas and (not isinstance(payload[2], list) or not 1 <= len(payload[2]) <= MAX_AREAS):
        raise ValueError(tr('区域数量应为 1 至 {0} 个', MAX_AREAS))
    return points, links, payload[2] if with_areas else []


def encode(scheme: Scheme, *, chinese=False) -> str:
    indices = {marker.id: i for i, marker in enumerate(scheme.markers)}
    if len(indices) != len(scheme.markers):
        raise ValueError(tr("方案中的标点编号重复"))
    points = []
    for marker in scheme.markers:
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in (marker.cx, marker.cy)):
            raise ValueError(tr("方案中存在超出地图范围的标点"))
        points.append([marker.type, round(marker.cx * 10000), round(marker.cy * 10000)])
    try:
        links = [[indices[link.from_id], indices[link.to_id], link.enabled] for link in scheme.paths]
    except KeyError as exc:
        raise ValueError(tr("方案存在找不到端点的连线")) from exc
    payload = [points, links]
    with_areas = bool(scheme.areas)
    if with_areas:
        if len(scheme.areas) > MAX_AREAS:
            raise ValueError(tr('每套方案最多分享 {0} 个区域', MAX_AREAS))
        payload.append([area.to_share() for area in scheme.areas])
    points, links, areas = _validate(payload, with_areas=with_areas)
    extended = any(shape == "circle" and not share_binary.circle_fits_wt3(geometry)
                   for _, shape, geometry in areas)
    binary = share_binary.pack(points, links, areas)
    compressor = zlib.compressobj(9, wbits=-15)
    deflated = compressor.compress(binary) + compressor.flush()
    body = min((b"\x00" + binary, b"\x01" + deflated), key=len)
    code = _packet_code(EXTENDED_PREFIX if extended else COMPACT_PREFIX, body)
    if not extended:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_RAW:
            raise ValueError(tr("方案太大，无法生成分享码"))
        prefix = AREA_PREFIX if with_areas else PREFIX
        legacy = _packet_code(prefix, zlib.compress(raw, 9))
        code = min((legacy, code), key=len)
    if len(code) > MAX_CODE:
        raise ValueError(tr("方案太大，无法生成分享码"))
    return share_text.encode(code) if chinese else code


def _packet_code(prefix, body):
    packet = zlib.crc32(body).to_bytes(4, "big") + body
    return prefix + base64.urlsafe_b64encode(packet).decode("ascii").rstrip("=")


def _inflate(data, *, wbits=15):
    unpacker = zlib.decompressobj(wbits)
    raw = unpacker.decompress(data, MAX_RAW + 1)
    if len(raw) > MAX_RAW or not unpacker.eof or unpacker.unused_data:
        raise ValueError(tr("分享码内容过大或不完整"))
    return raw


def decode(code: str, scene: str) -> Scheme:
    if len(code) > MAX_CODE:
        raise ValueError(tr("分享码过长"))
    compact = "".join(code.split())
    if compact.startswith(share_text.PREFIX):
        compact = share_text.decode(compact)
        if len(compact) > MAX_CODE:
            raise ValueError(tr("分享码过长"))
    prefix = compact[:len(PREFIX)]
    if prefix not in (PREFIX, AREA_PREFIX, COMPACT_PREFIX, EXTENDED_PREFIX):
        raise ValueError(tr("请粘贴完整的 WT1. 至 WT4. 分享码，或使用支持该版本的终端"))
    extended = prefix == EXTENDED_PREFIX
    try:
        token = compact[len(prefix):]
        packet = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        checksum, compressed = packet[:4], packet[4:]
        if len(packet) < 5 or int.from_bytes(checksum, "big") != zlib.crc32(compressed):
            raise ValueError(tr("分享码校验失败，请重新复制"))
        if prefix in (COMPACT_PREFIX, EXTENDED_PREFIX):
            mode, body = compressed[0], compressed[1:]
            if mode not in (0, 1):
                raise ValueError(tr("分享码压缩格式无效"))
            raw = _inflate(body, wbits=-15) if mode else body
            payload = share_binary.unpack(raw, MAX_POINTS, MAX_LINKS, allow_extended=extended)
        else:
            payload = json.loads(_inflate(compressed))
        with_areas = prefix in (AREA_PREFIX, EXTENDED_PREFIX) or (prefix == COMPACT_PREFIX and len(payload) == 3)
        points, links, areas = _validate(payload, with_areas=with_areas)
        decoded_areas = [Area.from_share(record, allow_extended=extended) for record in areas]
    except (binascii.Error, zlib.error, UnicodeError, RecursionError) as exc:
        raise ValueError(tr("分享码损坏，请重新复制")) from exc
    scheme = Scheme.fresh(scene, "导入的方案")
    scheme.markers = [Marker.new(kind, x / 10000, y / 10000) for kind, x, y in points]
    scheme.paths = [PathLink(scheme.markers[a].id, scheme.markers[b].id, enabled)
                    for a, b, enabled in links]
    scheme.areas = decoded_areas
    return scheme
