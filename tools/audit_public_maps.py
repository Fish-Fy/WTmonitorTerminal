"""对现有底图与公开底图进行全图比较，生成可复核的识别结果。"""
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agent.map_identity import file_identity, same_map
from sync_public_maps import WORK, image_entries


if __name__ == '__main__':
    sources = []
    for entry in image_entries():
        path = WORK / 'images' / Path(entry['path']).name
        identity = file_identity(path)
        if identity:
            sources.append((entry, identity))
    results = []
    for directory in sorted((ROOT / 'data/maps').iterdir()):
        identity = file_identity(directory / 'base.png')
        if identity is None:
            continue
        scores = sorted([(float(np.abs(identity.coarse - other.coarse).mean()), entry, other)
                         for entry, other in sources], key=lambda value: value[0])
        candidates = [dict(entry, distance=round(distance, 4), exact=same_map(identity, other))
                      for distance, entry, other in scores[:3]]
        results.append({'scene': directory.name, 'candidates': candidates})
    (WORK / 'audit.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    for result in results:
        candidate = result['candidates'][0]
        print(result['scene'], candidate['level'], candidate['ground'], candidate['distance'], candidate['exact'])
