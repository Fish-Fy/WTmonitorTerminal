"""地图图像下载、缓存、类型元数据与去重。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from stat import S_ISREG
import tempfile
import threading
import uuid
import zlib
from concurrent.futures import CancelledError
from functools import lru_cache, wraps
from pathlib import Path

from PIL import Image

from ..i18n import tr
from .map_identity import coarse_image, coarse_match, file_identity, image_identity, same_map

log = logging.getLogger(__name__)

# 旧版遗留的伪场景目录(不是真地图), 不进地图库
DUP_SKIP: frozenset[str] = frozenset({"in_progress", "running", "_unknown"})
KINDS: tuple[str, ...] = ("ground", "air", "naval", "unknown")
KIND_RULE = "visible_runway_v1"
CACHE_LOCK = threading.RLock()


def synchronized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with CACHE_LOCK:
            return function(*args, **kwargs)
    return wrapped


def atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件完整写完后替换，失败时保留原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cache-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_scene_name(scene: str | None) -> str:
    safe = str(scene or "_unknown").strip() or "_unknown"
    for ch in '<>:"/\\|?*':
        safe = safe.replace(ch, "_")
    safe = "".join("_" if ord(ch) < 32 else ch for ch in safe).rstrip(". ") or "_unknown"
    if safe.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                      *(f"LPT{i}" for i in range(1, 10))}:
        safe = "_" + safe
    return safe


def scene_path(data_root: Path, scene: str | None) -> Path:
    """场景名 → 缓存目录**路径**, **不创建目录**。

    改名逻辑必须用它: `scene_dir()` 会 mkdir, 于是改名时先造出一个空的目标目录 —— 那正是
    用户 2026-10-13 报的"空白副本", 而且这个空壳还会挡住下一次同名改名。
    """
    return data_root / "maps" / safe_scene_name(scene)


def scene_dir(data_root: Path, scene: str | None) -> Path:
    """给定场景名返回其缓存目录; scene 为空时用 '_unknown'。需要时会创建目录。"""
    d = scene_path(data_root, scene)
    d.mkdir(parents=True, exist_ok=True)
    return d


def map_cache_path(data_root: Path, scene: str | None) -> Path:
    """当前地图底图缓存文件(只存首张作为底图)。"""
    return scene_path(data_root, scene) / "base.png"


def scene_variants(data_root: Path, scene: str) -> list[Path]:
    """同一伪场景名下的所有目录（伪名撞车时会有 `-2` / `-3` 变体）。"""
    root = data_root / "maps"
    out: list[Path] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if d.is_dir() and (d.name == scene or d.name.startswith(f"{scene}-")):
            out.append(d)
    return out


def ensure_base_cache(data_root: Path, scene: str | None, img_bytes: bytes) -> Path:
    """缓存底图并返回**实际**文件路径。

    ⚠ 伪场景名是几何哈希且只量化到 2 位有效数字, **会撞车**（两张不同的图算出同一个
    `map_xxxx`）。以前这里是"只写一次", 撞车时**后来那张直接被丢掉** —— 用户觉得
    "玩过的地图不止这些"就是这个原因。现在改成:
      1) 全库已有**内容相同**的底图 ⇒ 复用，人工名字优先;
      2) 有目录但没底图 ⇒ 写进去;
      3) 内容不同 ⇒ 另存 `<scene>-2`、`<scene>-3` …, **一张都不丢**。

    调用方必须用返回路径的**目录名**当场景名(见 poller), 否则两张图的方案会串在一起。
    """
    base = safe_scene_name(scene)
    fp = hashlib.md5(img_bytes).hexdigest()
    if not img_bytes:
        raise ValueError("空底图不能写入地图缓存")
    # 与地图库整理使用同一标准：字节相同，或全幅地形一致的不同分辨率/压缩版本。
    identity = image_identity(img_bytes)
    while True:
        snapshot = library_snapshot(data_root)
        try:
            matches = []
            for path, size, modified in snapshot:
                if size <= 0:
                    continue
                candidate_fp = file_fingerprint(path)
                if (candidate_fp == fp or
                        (identity and coarse_match(_indexed_coarse(path, candidate_fp), identity.coarse)
                         and same_map(file_identity(path), identity))):
                    matches.append(path.parent.name)
        except OSError:
            if snapshot != library_snapshot(data_root):
                continue
            raise
        with CACHE_LOCK:
            if snapshot == library_snapshot(data_root):
                return _store_matched_base(data_root, base, img_bytes, fp, matches)


def _store_matched_base(data_root, base, img_bytes, fp, matches):
    if matches:
        keep = sorted(matches, key=lambda name: retention_rank(data_root, name))[0]
        # 相似关系不具传递性；来源必须也与保留图一致，不能经中间版本把不同图串起来。
        keep_path = map_cache_path(data_root, keep)
        keep_fp, keep_identity = file_fingerprint(keep_path), file_identity(keep_path)
        sources = []
        accepted = [(keep_fp, keep_identity)]
        for name in sorted(matches, key=lambda name: retention_rank(data_root, name)):
            if name == keep:
                continue
            path = map_cache_path(data_root, name)
            source_fp, source_identity = file_fingerprint(path), file_identity(path)
            if all(source_fp == old_fp or same_map(source_identity, old_identity)
                   for old_fp, old_identity in accepted):
                sources.append(name)
                accepted.append((source_fp, source_identity))
        # 只接收还没底图的原始条目的方案，不能把撞车的另一张图方案搬过来。
        raw = scene_path(data_root, base).name
        if raw != keep and raw not in sources and not map_cache_path(data_root, raw).is_file():
            if scene_path(data_root, raw).exists() or _scheme_path(data_root, raw).is_file():
                sources.append(raw)
        if sources:
            try:
                _merge_into(data_root, keep, sources)
            except (OSError, ValueError, TypeError):
                log.exception("自动合并失败，保留来源数据并继续使用已命名底图: %s", sources)
        _upgrade_base(keep_path, img_bytes)
        return map_cache_path(data_root, keep)
    variants = scene_variants(data_root, base)

    for d in variants:                       # 1) 已经是这张图了
        p = d / "base.png"
        if p.is_file() and file_fingerprint(p) == fp:
            return p

    for d in variants:                       # 2) 有目录没底图, 正好用它
        p = d / "base.png"
        if not p.exists():
            atomic_write(p, img_bytes)
            log.info("已缓存底图: %s", p)
            return p

    if not variants:                         # 3) 全新地图
        p = map_cache_path(data_root, base)
        atomic_write(p, img_bytes)
        log.info("已缓存底图: %s", p)
        return p

    i = 2                                    # 4) 撞车: 另开变体, 绝不覆盖也别丢
    while True:
        d = data_root / "maps" / f"{base}-{i}"
        p = d / "base.png"
        if p.is_file() and file_fingerprint(p) == fp:
            return p
        if not p.exists():
            d.mkdir(parents=True, exist_ok=True)
            atomic_write(p, img_bytes)
            log.info("伪场景名撞车, 另存为: %s", p)
            return p
        i += 1


def meta_path(data_root: Path, scene: str | None) -> Path:
    """地图元数据文件路径 —— **不创建目录**。

    以前这里走 `scene_dir()`（会 mkdir），于是**只要读一次 meta 就凭空建出一个空的地图目录**。
    用户 2026-10-17 报"地图库又出现空图"就是这么来的（读 meta 极频繁，几乎每帧都可能读）。
    """
    return scene_path(data_root, scene) / "meta.json"


def read_meta(data_root: Path, scene: str | None) -> dict:
    """读元数据; 缺失/损坏一律当空 dict(不抛)。**只读，不建目录。**"""
    try:
        raw = json.loads(meta_path(data_root, scene).read_text(encoding="utf-8-sig"))
        return raw if isinstance(raw, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


@synchronized
def write_meta(data_root: Path, scene: str | None, patch: dict) -> None:
    """合并写入元数据(读-改-写); 失败静默 —— 元数据丢了不该影响对局。"""
    m = read_meta(data_root, scene)
    m.update(patch or {})
    p = meta_path(data_root, scene)
    try:
        atomic_write(p, json.dumps(m, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError:
        pass


def map_kinds(data_root: Path, scene: str) -> list[str]:
    """地图分类；兼容旧累加标签及单值 kind。两用仍可手动设置。"""
    m = read_meta(data_root, scene)
    ks = m.get("kinds")
    if isinstance(ks, list):
        return [str(k) for k in ks if str(k) in ("ground", "air", "naval")]
    k = str(m.get("kind") or "")
    return [k] if k in ("ground", "air", "naval") else []


def map_kind(data_root: Path, scene: str) -> str:
    """首选类型(给只关心一个标签的地方用): ground / air / naval / unknown。"""
    ks = map_kinds(data_root, scene)
    return ks[0] if ks else "unknown"


def add_map_kind(data_root: Path, scene: str, kind: str) -> list[str]:
    """自动判定: 把 kind **累加**进标签。手工标记过的图不再被自动改写。"""
    if kind not in ("ground", "air", "naval"):
        return map_kinds(data_root, scene)
    if read_meta(data_root, scene).get("manual"):
        return map_kinds(data_root, scene)
    ks = map_kinds(data_root, scene)
    if kind not in ks:
        ks.append(kind)
        write_meta(data_root, scene, {"kinds": ks})
    return ks


@synchronized
def set_map_kind(data_root: Path, scene: str, kind: str) -> None:
    """手动校正: 设为该类型并打 manual 标记(此后自动判定不再覆盖, 用户说了算)。"""
    kinds = ["ground", "air"] if kind == "both" else [kind] if kind in ("ground", "air", "naval") else []
    write_meta(data_root, scene, {"kinds": kinds, "manual": True})
    saved = read_meta(data_root, scene)
    if saved.get("kinds") != kinds or not saved.get("manual"):
        raise OSError(tr("地图分类未能保存，请检查数据目录是否可写"))


@synchronized
def remember_auto_kind(data_root: Path, scene: str, kind: str) -> None:
    """跑道判定替换旧自动标签；手动优先，已证实的跑道不因缺帧降级。"""
    if kind not in ("ground", "air") or not map_cache_path(data_root, scene).is_file():
        return
    meta = read_meta(data_root, scene)
    if meta.get("manual"):
        return
    if meta.get("kind_rule") == KIND_RULE and "air" in map_kinds(data_root, scene):
        kind = "air"
    if meta.get("kind_rule") != KIND_RULE or meta.get("kinds") != [kind]:
        write_meta(data_root, scene, {"kinds": [kind], "kind_rule": KIND_RULE})


def file_fingerprint(path: Path, chunk: int = 1 << 20) -> str:
    """底图内容指纹(md5): 用来发现"同一张图被存成多个伪名"重复项。"""
    try:
        stat = path.stat()
        if not stat.st_size:
            return ""
        return _cached_fingerprint(str(path.resolve()), stat.st_size, stat.st_mtime_ns, chunk)
    except OSError:
        return ""


@lru_cache(maxsize=512)
def _cached_fingerprint(path: str, size: int, modified: int, chunk: int) -> str:
    h = hashlib.md5()
    with Path(path).open("rb") as stream:
        while data := stream.read(chunk):
            h.update(data)
    return h.hexdigest()


@lru_cache(maxsize=4096)
def _indexed_coarse(path: Path, fingerprint: str):
    """跨启动复用内容指纹对应的 32px 粗筛图。通过粗筛的地图仍须完整比对。"""
    index = path.parents[2] / ".map-index-v1" / (fingerprint + ".z")
    try:
        raw = zlib.decompress(index.read_bytes())
        if not raw:
            return None  # 透明底图等只参与精确指纹匹配。
        if len(raw) == 32 * 32 * 3:
            return Image.frombytes("RGB", (32, 32), raw)
    except (OSError, zlib.error):
        pass
    data = path.read_bytes()
    coarse = coarse_image(data)
    if fingerprint != hashlib.md5(data).hexdigest():
        # 文件在取指纹和解码之间被替换；调用方会用新快照重试。
        raise OSError(f"底图在读取期间发生变化: {path}")
    try:
        atomic_write(index, zlib.compress(coarse.tobytes() if coarse is not None else b"", level=1))
    except OSError:
        pass  # 缓存只用于加速，只读目录也能完成匹配。
    return coarse


def guess_kind(objs) -> str:
    """按整张地图是否有可见机场跑道分类，与对局模式/玩家载具无关。

    陆战小图也会收到图外机场，只认可与 [0,1] 底图相交的非零跑道线段。
    空帧不能证明没有跑道，不修改分类；不按当前缩放窗口裁剪。
    """
    objects = list(objs or ())
    if not objects:
        return "unknown"
    return "air" if any(_visible_runway(o) for o in objects) else "ground"


def _visible_runway(obj) -> bool:
    if str(getattr(obj, "type", "")).lower() not in ("airfield", "runway"):
        return False
    try:
        sx, sy, ex, ey = (float(getattr(obj, k)) for k in ("sx", "sy", "ex", "ey"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    if not all(math.isfinite(v) for v in (sx, sy, ex, ey)):
        return False
    if math.hypot(ex - sx, ey - sy) <= 1e-9:
        return False
    low, high = 0.0, 1.0
    for start, end in ((sx, ex), (sy, ey)):
        delta = end - start
        if abs(delta) <= 1e-12:
            if not 0.0 <= start <= 1.0:
                return False
            continue
        enter, leave = sorted((-start / delta, (1.0 - start) / delta))
        low, high = max(low, enter), min(high, leave)
        if high <= low:
            return False
    return high > low


@synchronized
def list_maps(data_root: Path, kind: str | None = None, skip_dups: bool = False
              ) -> list[tuple[str, Path | None]]:
    """列出 data/maps 下所有地图 -> [(场景名, 底图路径 或 None)]，按名字排序。

    * 排除旧版遗留的伪场景目录(in_progress / running / _unknown) —— 它们不是真地图;
    * `kind` 给 "ground"/"air"/"unknown" 时按 meta.json 记录的类型过滤;
    * `skip_dups=True` 时按**底图内容指纹**去重(同一张图只留一个)。

    无底图或零字节底图不列出；maintain_library 在启动和刷新时自动清理其缓存目录，
    关联的独立方案文件保留。8111 没有地图清单接口，底图只能靠对局缓存。
    """
    root = data_root / "maps"
    out: list[tuple[str, Path | None]] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name.startswith(".") or d.name in DUP_SKIP:
            continue
        base = d / "base.png"
        if not base.is_file() or base.stat().st_size == 0:
            continue
        if kind is not None:
            ks = map_kinds(data_root, d.name)
            if kind == "unknown":
                if ks:
                    continue
            elif kind not in ks:
                continue                 # 按**标签**匹配: 两用图在两个档里都会出现
        out.append((d.name, base if base.is_file() else None))
    if skip_dups:
        visible = {name for name, _ in out}
        hidden = set()
        for names in duplicate_groups(data_root).values():
            hidden.update([name for name in names if name in visible][1:])
        out = [(name, path) for name, path in out if name not in hidden]
    return out


def _cache_dirs(data_root: Path) -> list[Path]:
    root = data_root / "maps"
    if not root.is_dir():
        return []
    root = root.resolve()
    return [d for d in sorted(root.iterdir(), key=lambda p: p.name.casefold())
            if d.is_dir() and not d.name.startswith(".") and d.name not in DUP_SKIP
            and not d.is_symlink() and not d.is_junction() and d.resolve().parent == root]


def library_snapshot(data_root: Path) -> tuple:
    """同时记录空目录，以便在提交后台结果前发现新增、改名、替换或删除。"""
    entries = []
    for directory in _cache_dirs(data_root):
        path = directory / "base.png"
        try:
            stat = path.stat()
            entries.append((path, stat.st_size if S_ISREG(stat.st_mode) else 0, stat.st_mtime_ns))
        except FileNotFoundError:
            entries.append((path, 0, 0))
    return tuple(entries)


def _scheme_path(data_root: Path, scene: str) -> Path:
    return data_root / "schemes" / f"{scene_path(data_root, scene).name}.json"


def _scheme_records(data_root: Path, scene: str) -> list[dict]:
    """合并时严格读取；损坏的数据不能被当作空方案后删除。"""
    path = _scheme_path(data_root, scene)
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    records = raw if isinstance(raw, list) else raw.get("schemes") if isinstance(raw, dict) else None
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError(tr("方案文件格式异常: {0}", path))
    from ..store.markers import decode_schemes
    decode_schemes(records)                  # 图元损坏也不能当作可安全合并的数据
    return records


def retention_rank(data_root: Path, name: str) -> tuple:
    """人工命名永远优先；空方案文件不能压过真实名字。"""
    meta = read_meta(data_root, name)
    named = bool(meta.get("user_named")) or (not name.startswith("map_")
                                            and not name.startswith("未命名") and name not in DUP_SKIP)
    try:
        populated = any(record.get("markers") or record.get("paths") for record in _scheme_records(data_root, name))
    except (OSError, ValueError, TypeError):
        populated = False
    return (not named, not populated, name.casefold(), name)


@synchronized
def resolve_scene(data_root: Path, scene: str | None) -> str | None:
    """只解析已经合并删除的名字，现存撞车目录必须仍各自独立。"""
    if not scene or scene_path(data_root, scene).exists():
        return scene
    matches = [d.name for d in _cache_dirs(data_root)
               if scene in read_meta(data_root, d.name).get("aliases", [])]
    return matches[0] if len(matches) == 1 else scene


def _removable_cache_dir(data_root: Path, name: str) -> bool:
    directory = scene_path(data_root, name)
    root = data_root / "maps"
    if not directory.exists():
        return True
    if (not directory.is_dir() or directory.is_symlink() or directory.is_junction()
            or directory.resolve().parent != root.resolve()):
        return False
    return all(child.is_file() and not child.is_symlink()
               and child.name in {"base.png", "thumb.png", "meta.json"} for child in directory.iterdir())


def _remove_cache_dir(data_root: Path, name: str) -> None:
    if not _removable_cache_dir(data_root, name):
        raise ValueError(f"缓存目录含额外文件或链接，保留: {name}")
    directory = scene_path(data_root, name)
    if directory.is_dir():
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


@synchronized
def remove_library_map(data_root: Path, name: str) -> Path:
    """整目录移出地图库，保留可恢复副本与独立保存的标点方案。"""
    if not name or name.startswith(".") or safe_scene_name(name) != name or name in DUP_SKIP:
        raise ValueError(tr("无效的地图名称"))
    source = scene_path(data_root, name)
    if not _removable_cache_dir(data_root, name):
        raise ValueError(tr("地图目录含额外文件或链接，无法安全移除"))
    if not (source / "base.png").is_file():
        raise FileNotFoundError(tr("地图已移除或底图不存在"))

    removed = source.parent / ".removed"
    if removed.is_symlink() or removed.is_junction() or removed.resolve().parent != source.parent.resolve():
        raise ValueError(tr("地图备份目录必须位于地图库内"))
    removed.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=removed))
    destination = folder / name
    try:
        source.rename(destination)
    except OSError:
        folder.rmdir()
        raise
    return destination


@synchronized
def rename_map_data(data_root: Path, old: str, new: str) -> None:
    """先完整复制方案/记录再改名目录；任何提交失败都保留可恢复的原数据。"""
    if not new or safe_scene_name(new) != new or new in DUP_SKIP:
        raise ValueError(tr("地图名包含不支持的字符或保留名称"))
    src, dst = scene_path(data_root, old), scene_path(data_root, new)
    if src == dst:
        raise ValueError(tr("新旧地图名相同"))
    maps_root = (data_root / "maps").resolve()
    if (src.resolve().parent != maps_root or dst.resolve().parent != maps_root
            or src.is_symlink() or src.is_junction() or dst.is_symlink() or dst.is_junction()):
        raise ValueError(tr("地图目录必须位于当前地图库内"))
    old_sf, new_sf = _scheme_path(data_root, old), _scheme_path(data_root, new)
    old_kf = data_root / "killlines" / f"{safe_scene_name(old)}.jsonl"
    new_kf = data_root / "killlines" / f"{new}.jsonl"
    if not src.is_dir() and not old_sf.is_file():
        raise FileNotFoundError(tr("「{0}」不存在，可能已被清理或合并", old))
    if new_sf.exists() or new_kf.exists():
        raise FileExistsError(tr("「{0}」已有方案或击杀记录，请使用其它名字", new))
    if dst.exists() and ((dst / "base.png").exists() or not _removable_cache_dir(data_root, new)):
        raise FileExistsError(tr("「{0}」已存在或含有额外文件", new))
    records = _scheme_records(data_root, old)
    for record in records:
        record["scene"] = new
    meta_file = src / "meta.json"
    original_meta = meta_file.read_bytes() if meta_file.is_file() else None
    meta = read_meta(data_root, old)
    aliases = {value for value in meta.get("aliases", []) if isinstance(value, str)}
    meta.update(user_named=True, aliases=sorted((aliases | {old}) - {new}))
    created = []
    moved = False
    try:
        if old_sf.is_file():
            atomic_write(new_sf, json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"))
            created.append(new_sf)
        if old_kf.is_file():
            atomic_write(new_kf, old_kf.read_bytes())
            created.append(new_kf)
        if dst.exists():
            _remove_cache_dir(data_root, new)
        if src.is_dir():
            src.rename(dst)
            moved = True
            atomic_write(dst / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        from .mapstatics import rebind_scenes
        rebind_scenes(data_root, new, [old])  # 最后一个可失败提交步骤，来源文件此时仍完整
    except Exception:
        if moved:
            dst.rename(src)
            if original_meta is None:
                (src / "meta.json").unlink(missing_ok=True)
            else:
                atomic_write(src / "meta.json", original_meta)
        for path in created:
            path.unlink(missing_ok=True)
        raise
    for path in (old_sf, old_kf):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("改名成功但旧记录未能清理，保留副本: %s", path, exc_info=True)


def _merge_into(data_root: Path, keep: str, sources: list[str]) -> list[str]:
    """目标方案/元数据/击杀记录全部落盘后才清理来源；失败可重试。"""
    if not all(_removable_cache_dir(data_root, name) for name in sources):
        raise ValueError("重复目录含额外文件，跳过自动合并")
    schemes = _scheme_records(data_root, keep)
    meta = read_meta(data_root, keep)
    layouts = {str(layout["id"]): layout for layout in meta.get("public_layouts", [])
               if isinstance(layout, dict) and "id" in layout}
    aliases = set(meta.get("aliases", []))
    kinds = set(map_kinds(data_root, keep))
    manual = bool(meta.get("manual"))
    # 保留原始文本行，不截断历史；重复运行也不会把相同行重复追加。
    kill_path = data_root / "killlines" / f"{keep}.jsonl"
    kill_rows = kill_path.read_text(encoding="utf-8-sig").splitlines() if kill_path.is_file() else []
    kill_seen = set(kill_rows)
    for other in sources:
        for record in _scheme_records(data_root, other):
            record = dict(record)
            same_id = [item for item in schemes if item.get("id") == record.get("id")]
            content = {k: v for k, v in record.items() if k not in {"scene", "name", "id"}}
            if any({k: v for k, v in item.items() if k not in {"scene", "name", "id"}} == content
                   for item in same_id):
                continue
            if same_id:
                # 同 id 不同内容都要留下；重试时用内容检查避免再追加。
                if any(item.get("_merged_from") == [other, record.get("id")]
                       and {k: v for k, v in item.items() if k not in {"scene", "name", "id", "_merged_from"}} == content
                       for item in schemes):
                    continue
                record["_merged_from"] = [other, record.get("id")]
                record["id"] = uuid.uuid4().hex[:12]
            record["scene"] = keep
            record["name"] = f"{record.get('name', '未命名方案')}（来自 {other}）"
            schemes.append(record)
        other_meta = read_meta(data_root, other)
        for layout in other_meta.get("public_layouts", []):
            if not isinstance(layout, dict) or "id" not in layout:
                continue
            key = str(layout["id"])
            existing = layouts.get(key)
            if existing is not None and (existing.get("source"), existing.get("objects")) != (
                    layout.get("source"), layout.get("objects")):
                raise ValueError("重复地图的任务布局冲突，保留来源")
            layouts.setdefault(key, layout)
        aliases.update(other_meta.get("aliases", []))
        aliases.add(other)
        if not manual:
            other_kinds = set(map_kinds(data_root, other))
            if other_meta.get("manual"):
                kinds = other_kinds
                manual = True
                meta["manual"] = True
            elif other_meta.get("kind_rule") == KIND_RULE:
                if meta.get("kind_rule") == KIND_RULE:
                    kinds = {"air"} if "air" in kinds | other_kinds else {"ground"}
                else:
                    kinds = other_kinds
                meta["kind_rule"] = KIND_RULE
            elif meta.get("kind_rule") != KIND_RULE:
                kinds.update(other_kinds)
        other_kills = data_root / "killlines" / f"{other}.jsonl"
        if other_kills.is_file():
            for row in other_kills.read_text(encoding="utf-8-sig").splitlines():
                if row and row not in kill_seen:
                    kill_rows.append(row)
                    kill_seen.add(row)
    if schemes:
        atomic_write(_scheme_path(data_root, keep), json.dumps(schemes, ensure_ascii=False, indent=2).encode("utf-8"))
        if _scheme_records(data_root, keep) != schemes:
            raise OSError("合并方案验证失败")
    if layouts:
        meta["public_layouts"] = list(layouts.values())
    meta.update({"aliases": sorted(aliases - {keep}), "kinds": sorted(kinds)})
    # 保留者手动校正过的分类不能被来源图改掉。
    atomic_write(meta_path(data_root, keep), json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    if kill_rows:
        atomic_write(kill_path, ("\n".join(kill_rows) + "\n").encode("utf-8"))
    from .mapstatics import rebind_scenes
    rebind_scenes(data_root, keep, sources)
    keep_path = map_cache_path(data_root, keep)
    for other in sources:
        source_path = map_cache_path(data_root, other)
        if source_path.is_file():
            _upgrade_base(keep_path, source_path.read_bytes())
    removed = []
    for other in sources:
        _remove_cache_dir(data_root, other)
        # 方案已完整验证迁入目标，来源文件不再留下孤儿副本。
        _scheme_path(data_root, other).unlink(missing_ok=True)
        (data_root / "killlines" / f"{other}.jsonl").unlink(missing_ok=True)
        removed.append(other)
    return removed


def _upgrade_base(path: Path, data: bytes) -> None:
    """保留人工名字，并在同图匹配后保留更高清底图；旧缩略图随之失效。"""
    current, incoming = file_identity(path), image_identity(data)
    if (current is not None and incoming is not None and same_map(current, incoming)
            and incoming.size[0] * incoming.size[1] > current.size[0] * current.size[1]):
        atomic_write(path, data)
        path.with_name("thumb.png").unlink(missing_ok=True)


def matching_pairs(snapshot: tuple, cancel=None) -> dict[str, set[str]]:
    """只读计算。粗筛仅排除不可能匹配的底图，候选仍须通过全幅比对。"""
    previous = []
    neighbors = {}
    for path, size, modified in snapshot:
        if cancel is not None and cancel.is_set():
            raise CancelledError()
        if size <= 0:
            continue
        fp = file_fingerprint(path)
        if not fp:
            raise OSError(f"无法读取底图: {path}")
        coarse = _indexed_coarse(path, fp)
        for other, other_fp, other_coarse in previous:
            if fp != other_fp and not (coarse_match(coarse, other_coarse)
                                       and same_map(file_identity(path), file_identity(other))):
                continue
            name, peer = path.parent.name, other.parent.name
            neighbors.setdefault(name, set()).add(peer)
            neighbors.setdefault(peer, set()).add(name)
        previous.append((path, fp, coarse))
    return neighbors


def _ranked_groups(data_root, neighbors):
    groups = []
    for name in sorted(neighbors, key=lambda n: retention_rank(data_root, n)):
        for group in groups:
            if all(peer in neighbors[name] for peer in group):
                group.append(name)
                break
        else:
            groups.append([name])
    return {file_fingerprint(map_cache_path(data_root, group[0])): group
            for group in groups if len(group) > 1}


@synchronized
def duplicate_groups(data_root: Path) -> dict[str, list[str]]:
    return _ranked_groups(data_root, matching_pairs(library_snapshot(data_root)))


@synchronized
def merge_duplicate_maps(data_root: Path) -> list[tuple[str, list[str]]]:
    """把"同一张底图、多个目录"的重复项合并成一个; 返回 [(保留, [被并入...])]。

    保留策略(依次): **人工命名** > **有实际标点的方案** > 名字序。
    被并入者的方案会搬到保留者名下(方案名加"(来自 xxx)"后缀), 随后删除其目录。
    启动和地图库刷新自动执行，手动按钮也使用同一排序和实现。
    """
    return _merge_groups(data_root, duplicate_groups(data_root))


def _merge_groups(data_root, groups):
    merged: list[tuple[str, list[str]]] = []
    for names in groups.values():
        keep, drop = names[0], names[1:]
        try:
            removed = _merge_into(data_root, keep, drop)
            merged.append((keep, removed))
        except (OSError, ValueError, TypeError):
            log.exception("合并失败，保留来源数据: %s", names)
    return merged


@synchronized
def maintain_library(data_root: Path) -> dict:
    """自动合并相同底图，清除无底图缓存目录；独立方案文件始终保留。"""
    snapshot = library_snapshot(data_root)
    return _maintain_snapshot(data_root, snapshot, matching_pairs(snapshot))


@synchronized
def apply_library_scan(data_root, snapshot, neighbors):
    if snapshot != library_snapshot(data_root):
        return None
    return _maintain_snapshot(data_root, snapshot, neighbors)


def _maintain_snapshot(data_root, snapshot, neighbors):
    merged = _merge_groups(data_root, _ranked_groups(data_root, neighbors))
    removed = []
    for base, size, _modified in snapshot:
        directory = base.parent
        if size:
            continue
        try:
            _remove_cache_dir(data_root, directory.name)
            removed.append(directory.name)
        except (OSError, ValueError):
            log.warning("无法清理无底图缓存目录，保留数据: %s", directory, exc_info=True)
    return {"merged": merged, "removed": removed}


def create_map_dir(data_root: Path, scene: str) -> Path:
    """建一个地图条目(目录), 供离线规划; 已存在则原样返回。"""
    return scene_dir(data_root, scene)
