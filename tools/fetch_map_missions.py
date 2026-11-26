"""读取公开任务的简报布局，保留原始任务供坐标校验。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import urllib.request
import sys

WORK = Path(__file__).resolve().parents[1] / '.tmp' / 'map-sync'
BASE = 'https://raw.githubusercontent.com/gszabi99/War-Thunder-Datamine/master/mis.vromfs.bin_u/'


def fetch(path):
    target = WORK / 'missions' / (path + 'x')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return path, None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(BASE + path + 'x', timeout=45) as response:
                data = response.read()
            json.loads(data)
            target.write_bytes(data)
            return path, None
        except Exception as error:
            last = str(error)
    return path, last


if __name__ == '__main__':
    catalog = json.loads((WORK / 'level_catalog.json').read_text(encoding='utf-8-sig'))
    paths = sorted({mission['mission_path'] for level in catalog['levels'].values()
                    for branch, missions in level.get('missions', {}).items()
                    if branch in ('tanks', 'planes', 'ships', 'helicopters')
                    for mission in missions.values()})
    if '--imports' in sys.argv:
        selected = set()
        for source in (WORK / 'missions').rglob('*.blkx'):
            raw = json.loads(source.read_text(encoding='utf-8-sig'))
            imports = raw.get('imports', {}).get('import_record', [])
            if isinstance(imports, dict):
                imports = [imports]
            for item in imports:
                path = item.get('file', '').lower()
                if path and any(word in path for word in ('bdt_bases', 'bomb', 'airfields', 'spawns')):
                    selected.add(path)
        paths = sorted(selected)
    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        for future in as_completed([pool.submit(fetch, path) for path in paths]):
            results.append(future.result())
            if len(results) % 100 == 0:
                print(f'{len(results)}/{len(paths)}', flush=True)
    (WORK / 'mission-downloads.json').write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8')
    print('完成', len(results), '失败', sum(error is not None for _, error in results), flush=True)
