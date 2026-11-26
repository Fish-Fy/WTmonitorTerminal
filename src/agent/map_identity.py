"""同一地图不同分辨率/压缩版本的保守比对，不对齐、不裁切、不旋转底图。"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from io import BytesIO
from pathlib import Path
from statistics import pstdev

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError


@dataclass(frozen=True, eq=False)
class ImageSummary:
    size: tuple[int, int]
    pixels: bytes | None
    coarse: Image.Image
    texture: float


@dataclass(frozen=True, eq=False)
class ImageIdentity(ImageSummary):
    fine: Image.Image


def _decoded_rgb(source):
    try:
        with Image.open(source) as image:
            width, height = image.size
            if min(width, height) < 512 or max(width, height) > 8192 or getattr(image, "n_frames", 1) != 1:
                return None
            if "A" in image.getbands() or "transparency" in image.info:
                if image.convert("RGBA").getchannel("A").getextrema() != (255, 255):
                    return None
            # JPEG 解码器直接输出较小版本，避免启动时逐张解码全库 2048/4096 底图。
            # draft 不裁切、不改坐标范围；只有未降采样时才提供精确像素指纹。
            image.draft("RGB", (512, 512))
            return (width, height), image.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def coarse_image(data: bytes) -> Image.Image | None:
    decoded = _decoded_rgb(BytesIO(data))
    return decoded[1].resize((32, 32), Image.Resampling.LANCZOS) if decoded else None


@lru_cache(maxsize=4)
def image_identity(data: bytes) -> ImageIdentity | None:
    """按完整底图的同一归一化范围比较；小占位图/损坏图不做相似合并。"""
    decoded = _decoded_rgb(BytesIO(data))
    if decoded is None:
        return None
    size, rgb = decoded
    pixels = hashlib.sha256(rgb.tobytes()).digest() if rgb.size == size else None
    coarse = rgb.resize((32, 32), Image.Resampling.LANCZOS)
    fine = rgb.resize((256, 256), Image.Resampling.LANCZOS)
    texture = pstdev(sum(pixel) for pixel in fine.getdata()) / 3
    return ImageIdentity(size, pixels, coarse, texture, fine)


def file_identity(path: Path) -> ImageIdentity | None:
    try:
        stat = path.stat()
        return _file_identity(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


@lru_cache(maxsize=128)
def _file_identity(path: str, size: int, modified: int) -> ImageIdentity | None:
    return image_identity(Path(path).read_bytes())


def coarse_match(a: Image.Image | None, b: Image.Image | None) -> bool:
    return a is not None and b is not None and sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 <= 1.2


def possible_match(a: ImageSummary | None, b: ImageSummary | None) -> bool:
    if a is None or b is None:
        return False
    if a.size == b.size and a.pixels is not None and a.pixels == b.pixels:
        return True
    if a.size[0] * b.size[1] != b.size[0] * a.size[1]:
        return False
    # 纯色/近纯色占位图没有足够的地形信息，不能用很小的色差判同图。
    if min(a.texture, b.texture) < 8.0:
        return False
    return coarse_match(a.coarse, b.coarse)


def same_map(a: ImageIdentity | None, b: ImageIdentity | None) -> bool:
    if not possible_match(a, b):
        return False
    if a.size == b.size and a.pixels is not None and a.pixels == b.pixels:
        return True
    delta = ImageChops.difference(a.fine, b.fine)
    stats = ImageStat.Stat(delta)
    if sum(stats.mean) / 3 > 2.5 or sum(stats.sum2) / (256 * 256 * 3) > 16.0:
        return False
    # 再核对 16×16 个局部区域及最大色差，避免平均值掩盖新增建筑/道路等差异。
    if max(high for low, high in stats.extrema) > 48:
        return False
    return all(sum(ImageStat.Stat(delta.crop((x, y, x + 16, y + 16))).mean) / 3 <= 5.0
               for y in range(0, 256, 16) for x in range(0, 256, 16))
