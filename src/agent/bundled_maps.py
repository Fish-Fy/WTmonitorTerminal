"""Apply the Sun City artwork shipped with the program update."""

import hashlib
import json
import logging
from pathlib import Path

from . import mapimg

SCENE = "日耀之城"
SUN_CITY_UPDATE = Path("map-updates/sun-city-2.61")
SUN_CITY_SHA256 = "726502ce7b53a7abef970f1055b98fce2d764ae2ef6a8dab5454ffd88f61ccf5"
OLD_SUN_CITY_SHA256 = {
    "a658bba768fcc52fb1692a9778f83861b1a4c876f50bf3c26da27a2f758b1658",
    "9c730a8715deaa513bfa188078dc3354eacf470b84252e1bbbbdeae791aa377e",
}
log = logging.getLogger(__name__)


@mapimg.synchronized
def install_sun_city(data_root: Path, resources: Path) -> None:
    supplied = resources / SUN_CITY_UPDATE
    applied = data_root / ".map-updates" / SUN_CITY_UPDATE.name
    if not supplied.is_dir() or applied.is_file():
        return
    try:
        base = mapimg.map_cache_path(data_root, SCENE)
        current = hashlib.sha256(base.read_bytes()).hexdigest() if base.is_file() else None
        if current is not None and current not in OLD_SUN_CITY_SHA256 and current != SUN_CITY_SHA256:
            mapimg.atomic_write(applied, b"custom\n")
            return
        image = (supplied / "base.png").read_bytes()
        if hashlib.sha256(image).hexdigest() != SUN_CITY_SHA256:
            raise ValueError("Unexpected Sun City map checksum")
        public = json.loads((supplied / "meta.json").read_text(encoding="utf-8"))
        thumbnail = (supplied / "thumb.png").read_bytes()
        meta = mapimg.read_meta(data_root, SCENE)
        for key in ("kinds", "kind_rule", "public_level", "public_layouts"):
            if key in public:
                meta.setdefault(key, public[key])
        for key in ("public_image", "public_source"):
            meta[key] = public[key]
        if current != SUN_CITY_SHA256:
            mapimg.atomic_write(base, image)
        preview = base.with_name("thumb.png")
        if not preview.is_file() or preview.stat().st_mtime_ns < base.stat().st_mtime_ns:
            mapimg.atomic_write(preview, thumbnail)
        mapimg.atomic_write(mapimg.meta_path(data_root, SCENE),
                            json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        mapimg.atomic_write(applied, b"installed\n")
    except (OSError, ValueError, KeyError):
        log.exception("Could not install the Sun City map update")
