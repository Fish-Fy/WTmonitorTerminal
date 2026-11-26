"""将已校验公开底图和任务简报导入个人地图库，先备份再改名。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sys
import unicodedata

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agent import mapimg
from sync_public_maps import WORK, image_entries

DATA = ROOT / 'data'
CATALOG = json.loads((WORK / 'level_catalog.json').read_text(encoding='utf-8-sig'))['levels']
DIFFICULTIES = {'realistic': '历史', 'arcade': '街机', 'hardcore': '全真'}


def clean(value):
    return ''.join(c for c in value if unicodedata.category(c) != 'Cf').strip()


def scene_name(entry):
    level = entry['level']
    names = CATALOG[level]['names']
    name = clean(names.get('Chinese') or names.get('English') or level)
    name = name.replace(' - 陆战', '').replace('- 陆战', '')
    if level == 'avg_falklands':
        name = '达尔文丘'
    if 'snow' in level or level.endswith('_w') or 'winter' in level:
        name += '（冬季）'
    if not entry['ground'] and level.startswith(('avg_', 'avn_')):
        name += '（空域）'
    if entry['ground'] and level.startswith('avn_'):
        name += '（海战）'
    if 'avg_maginot_' in entry['path']:
        name += '（旧版）'
    return mapimg.safe_scene_name(name)


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def briefing_difficulties(raw, mission, slide):
    difficulty = slide.get('difficulty', '')
    settings = raw.get('mission_settings', {}).get('mission', {})
    alternate = settings.get('useAlternativeMapCoord', mission.get('use_alternative_map_coord'))
    # A mission can restore the normal map for specific difficulties.
    overrides = settings.get('mission', [])
    if isinstance(overrides, dict):
        overrides = [overrides]
    by_mode = {row['difficulty']: row['useAlternativeMapCoord'] for row in overrides
               if row.get('difficulty') in DIFFICULTIES and 'useAlternativeMapCoord' in row}
    if difficulty:
        return [] if by_mode.get(difficulty, alternate) else [difficulty]
    standard = [mode for mode in DIFFICULTIES if not by_mode.get(mode, alternate)]
    return [''] if len(standard) == len(DIFFICULTIES) else standard


def build_layouts(entry):
    level = CATALOG[entry['level']]
    coordinates = level['level_def'].get('map_coords', {})
    prefix = 'tankMapCoord' if entry['ground'] else 'mapCoord'
    if prefix + '0' not in coordinates or prefix + '1' not in coordinates:
        return []
    lower, upper = coordinates[prefix + '0'], coordinates[prefix + '1']
    if lower and isinstance(lower[0], list):
        lower = lower[-1]
    if upper and isinstance(upper[0], list):
        upper = upper[-1]
    if len(lower) != 2 or len(upper) != 2:
        return []
    width, height = upper[0] - lower[0], upper[1] - lower[1]
    if width <= 0 or height <= 0:
        return []
    layouts = []
    for branch, missions in level.get('missions', {}).items():
        if branch not in ('tanks', 'planes', 'ships', 'helicopters'):
            continue
        if entry['ground'] and branch not in ('tanks', 'ships'):
            continue
        for key, mission in missions.items():
            source = WORK / 'missions' / (mission['mission_path'] + 'x')
            if not source.is_file():
                continue
            raw = json.loads(source.read_text(encoding='utf-8-sig'))
            areas = raw.get('areas', {})
            units = {obj['name']: obj for obj in dictionaries(raw.get('units', {}))
                     if obj.get('name') and 'tm' in obj} if branch in ('planes', 'ships') else {}
            bomb_areas = dict(areas)
            imports = raw.get('imports', {}).get('import_record', [])
            if isinstance(imports, dict):
                imports = [imports]
            for record in imports:
                imported = WORK / 'missions' / (record.get('file', '').lower() + 'x')
                if imported.is_file() and record.get('importAreas', True):
                    imported_raw = json.loads(imported.read_text(encoding='utf-8-sig'))
                    bomb_areas.update(imported_raw.get('areas', {}))
            bombs = []
            if not entry['ground']:
                for area_name, area in bomb_areas.items():
                    match = re.fullmatch(r'(?:bdt_)?t([12])_bomb_(?:area|zone)(?:_\d+)?', area_name)
                    matrix = area.get('tm', [])
                    if not match or len(matrix) != 4:
                        continue
                    x, _, z = matrix[3]
                    x, y = (x - lower[0]) / width, (upper[1] - z) / height
                    if 0 <= x <= 1 and 0 <= y <= 1:
                        bombs.append({'x': round(x, 6), 'y': round(y, 6),
                                      'type': 'defending_point' if match[1] == '1' else 'bombing_point'})
            briefing = raw.get('mission_settings', {}).get('briefing', {})
            for slide in dictionaries(briefing):
                if not slide.get('map') or not slide.get('icon'):
                    continue
                if entry['ground'] and slide.get('showAirMap') and branch != 'ships':
                    continue
                difficulties = briefing_difficulties(raw, mission, slide)
                if not difficulties:
                    continue
                icons = slide['icon'] if isinstance(slide['icon'], list) else [slide['icon']]
                objects = list(bombs)
                for icon in icons:
                    if not isinstance(icon, dict):
                        continue
                    kind = icon.get('icontype', '')
                    target_name = icon.get('target') or ''
                    target = areas.get(target_name)
                    if target is None and (kind == 'airfield' or
                                           (kind in ('ship', 'destroyer') and 'spawn' in target_name)):
                        target = units.get(target_name)
                    if target is None:
                        continue
                    matrix = target.get('tm', [])
                    if len(matrix) != 4 or len(matrix[3]) != 3:
                        continue
                    x, _, z = matrix[3]
                    x, y = (x - lower[0]) / width, (upper[1] - z) / height
                    if not (0 <= x <= 1 and 0 <= y <= 1):
                        continue
                    side = icon.get('side', '')
                    color = {'ally': '#174dff', 'enemy': '#fa0c00'}.get(side, '#ffffff')
                    item = {'x': round(x, 6), 'y': round(y, 6), 'color': color}
                    if kind.startswith(('basezone_', 'capturezone_')) or kind == 'basezone':
                        label = kind.split('_')[-1] if '_' in kind else ''
                        item.update(type='capture_zone', icon='capture_zone', zone_label=label)
                    elif kind in ('tank', 'airfield', 'fighter', 'bomber', 'ship', 'destroyer', 'helicopter'):
                        vehicle = {'tank': 'tank', 'ship': 'ship', 'helicopter': 'helicopter',
                                   'destroyer': 'ship', 'bomber': 'bomber'}.get(kind, 'fighter')
                        item.update(type='respawn_base_' + vehicle)
                    elif kind in ('bombing_point', 'bombing_zone', 'bomb_zone', 'bombing', 'base'):
                        item.update(type='defending_point' if side == 'ally' else 'bombing_point')
                    else:
                        continue
                    if item not in objects:
                        objects.append(item)
                if not objects:
                    continue
                name = clean(mission.get('names', {}).get('Chinese') or
                             mission.get('names', {}).get('English') or key)
                for difficulty in difficulties:
                    label = name + ' · ' + DIFFICULTIES.get(difficulty, '通用')
                    if bombs:
                        label += ' · 含候选轰炸区'
                    layouts.append({'id': key + '/' + difficulty, 'name': label,
                                    'source': mission['mission_path'], 'objects': objects,
                                    'difficulty': difficulty})
    layouts.sort(key=lambda layout: (layout['difficulty'] != 'realistic',
                                    not layout['id'].split('/')[0].endswith('_dom'),
                                    layout['name'], layout['id']))
    duplicates = defaultdict(list)
    for layout in layouts:
        duplicates[layout['name']].append(layout)
    for group in duplicates.values():
        if len(group) > 1:
            for index, layout in enumerate(group, 1):
                layout['name'] += f' · 布局 {index}'
    return layouts


def apply():
    backup = WORK / ('backup-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copytree(DATA, backup / 'data')
    print('备份', backup, flush=True)
    entries = image_entries()
    by_path = {entry['path']: entry for entry in entries}
    audit = json.loads((WORK / 'audit.json').read_text(encoding='utf-8'))
    crop_file = WORK / 'crop-audit.json'
    crop_matches = {}
    if crop_file.is_file():
        for row in json.loads(crop_file.read_text(encoding='utf-8')):
            candidate = row['candidates'][0]
            transform = candidate['transform']
            if (candidate['score'] >= .99 and transform['size'] == 128
                    and transform['x'] == 0 and transform['y'] == 0):
                crop_matches[row['scene']] = candidate
    matches = defaultdict(list)
    unresolved = []
    for row in audit:
        if row['scene'] in mapimg.DUP_SKIP:
            continue
        candidate = crop_matches.get(row['scene'], row['candidates'][0])
        if candidate.get('exact') or row['scene'] in crop_matches:
            matches[candidate['path']].append(row['scene'])
        else:
            unresolved.append(row['scene'])
    report = {'backup': str(backup), 'renamed': [], 'added': [], 'unresolved': unresolved,
              'layout_count': 0, 'with_layouts': 0}
    # 先用独立中间名释放名称，兼容用户原名称恰好被另一幅图占用的情况。
    staged = {}
    for number, (path, scenes) in enumerate(matches.items()):
        keep = sorted(scenes, key=lambda name: mapimg.retention_rank(DATA, name))[0]
        others = [name for name in scenes if name != keep]
        if others:
            mapimg._merge_into(DATA, keep, others)
        temporary = f'_public_import_{number}'
        mapimg.rename_map_data(DATA, keep, temporary)
        staged[path] = (temporary, keep)
    used = set(directory.name for directory in (DATA / 'maps').iterdir())
    for entry in entries:
        path = entry['path']
        name = scene_name(entry)
        if name in used:
            name += '（陆战）' if entry['ground'] else '（空战）'
        if name in used:
            name += '（另一版本）'
        used.add(name)
        if path in staged:
            temporary, old = staged[path]
            mapimg.rename_map_data(DATA, temporary, name)
            if old != name:
                report['renamed'].append([old, name])
        else:
            image_path = WORK / 'images' / Path(path).name
            with Image.open(image_path) as image:
                image.verify()
            mapimg.atomic_write(DATA / 'maps' / name / 'base.png', image_path.read_bytes())
            report['added'].append(name)
        layouts = build_layouts(entry)
        metadata = mapimg.read_meta(DATA, name)
        aliases = [alias for alias in metadata.get('aliases', []) if not alias.startswith('_public_import_')]
        kinds = ['ground'] if entry['ground'] and not entry['level'].startswith('avn_') else ['air']
        if entry['ground'] and entry['level'].startswith('avn_'):
            kinds = ['naval']
        metadata.update(aliases=aliases, public_level=entry['level'], public_image=path,
                        public_source='https://github.com/LivingTheDagor/WtMiniMapPictures',
                        public_layouts=layouts, kinds=kinds, manual=True)
        mapimg.atomic_write(DATA / 'maps' / name / 'meta.json',
                            json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'))
        with Image.open(DATA / 'maps' / name / 'base.png') as image:
            image.thumbnail((132, 132))
            image.save(DATA / 'maps' / name / 'thumb.png')
        report['layout_count'] += len(layouts)
        report['with_layouts'] += bool(layouts)
    (WORK / 'import-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: len(value) if isinstance(value, list) else value for key, value in report.items()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    apply()
