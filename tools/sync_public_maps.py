"""下载公开底图、核对本地身份并补齐地图库。下载与写入分阶段执行。"""
from __future__ import annotations

import concurrent.futures
import io
import json
from pathlib import Path
import sys
import time
import urllib.request

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = ROOT / '.tmp' / 'map-sync'
IMAGE_ROOT = 'https://raw.githubusercontent.com/LivingTheDagor/WtMiniMapPictures/main/'
ALIASES = {'avg_maginot': 'avg_sector_montmedy'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def image_entries():
    catalog = read(WORK / 'level_catalog.json')['levels']
    result = []
    tree = read(WORK / 'pictures-tree.json')['tree']
    available = {entry['path'] for entry in tree}
    for entry in tree:
        path = entry['path']
        if not path.startswith('2048/') or not path.endswith('.png'):
            continue
        stem = Path(path).stem
        suffix = next((s for s in ('_tankmap', '_planemap', '_airmap', '_map') if stem.endswith(s)), None)
        if not suffix:
            continue
        level = ALIASES.get(stem[:-len(suffix)], stem[:-len(suffix)])
        if level in catalog and catalog[level].get('names'):
            small = path.replace('2048/', '1024/')
            result.append({'path': small if small in available else path, 'level': level, 'ground': suffix == '_tankmap'})
    return result


def download(entry):
    path = WORK / 'images' / Path(entry['path']).name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return entry
    for attempt in range(3):
        try:
            with urllib.request.urlopen(IMAGE_ROOT + entry['path'], timeout=300) as response:
                data = response.read()
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            path.write_bytes(data)
            return entry
        except Exception as error:
            if attempt == 2:
                return dict(entry, error=str(error))
            time.sleep(1)


if __name__ == '__main__':
    entries = image_entries()
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        results = []
        for result in pool.map(download, entries):
            results.append(result)
            if len(results) % 20 == 0:
                print(f'下载 {len(results)}/{len(entries)}', flush=True)
    (WORK / 'downloads.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print('完成', len(results), '失败', sum('error' in x for x in results), flush=True)
