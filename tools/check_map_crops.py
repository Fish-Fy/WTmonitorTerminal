"""对未识别的缓存图进行缩放与平移相关性核验，不修改地图库。"""
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_public_maps import WORK, ROOT, image_entries


def gray(path, size):
    with Image.open(path) as image:
        return np.asarray(image.convert('L').resize((size, size)), dtype=float)


def correlation(image, template):
    h, w = template.shape
    height, width = image.shape
    shape = (height + h - 1, width + w - 1)
    centered = template - template.mean()
    dot = np.fft.irfft2(np.fft.rfft2(image, shape) * np.fft.rfft2(centered[::-1, ::-1], shape), s=shape)
    dot = dot[h - 1:height, w - 1:width]
    def sums(array):
        integral = np.pad(array, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]
    energy = sums(image ** 2) - sums(image) ** 2 / (h * w)
    result = dot / np.sqrt(np.maximum(energy * (centered ** 2).sum(), 1))
    y, x = np.unravel_index(result.argmax(), result.shape)
    return float(result[y, x]), int(x), int(y)


if __name__ == '__main__':
    rows = json.loads((WORK / 'audit.json').read_text(encoding='utf-8'))
    entries = image_entries()
    sources = [(entry, gray(WORK / 'images' / Path(entry['path']).name, 128)) for entry in entries]
    results = []
    for row in rows:
        if row['candidates'][0]['exact']:
            continue
        local = gray(ROOT / 'data/maps' / row['scene'] / 'base.png', 128)
        scores = []
        for entry, source in sources:
            for size in (64, 80, 96, 112, 128, 144, 160, 192, 224, 256):
                resized = np.asarray(Image.fromarray(source.astype('uint8')).resize((size, size)), dtype=float)
                if size <= 128:
                    score, x, y = correlation(local, resized)
                    transform = {'direction': 'source_in_local', 'size': size, 'x': x, 'y': y}
                else:
                    score, x, y = correlation(resized, local)
                    transform = {'direction': 'local_in_source', 'size': size, 'x': x, 'y': y}
                scores.append(dict(entry, score=round(score, 6), transform=transform))
        scores.sort(key=lambda entry: -entry['score'])
        result = {'scene': row['scene'], 'candidates': scores[:3]}
        results.append(result)
        print(row['scene'], scores[0], flush=True)
    (WORK / 'crop-audit.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
