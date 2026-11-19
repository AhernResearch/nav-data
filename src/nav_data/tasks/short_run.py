"""Prepare (freeze) and collect one short-instruction run against the sim-engine collection service."""

import copy
import shutil
from pathlib import Path

from nav_data.collection.client import run_remote
from nav_data.records import digest, read, write
from nav_data.tasks.pointnav import require_train
from nav_data.tasks.policy import short_concurrency, validate_distance_limits, visible_region_config


def prepare_short(source_config, output):
    """Freeze one new short-instruction run: config plus a nav-data source snapshot. Never launches."""


    source_config, output = Path(source_config), Path(output)
    config = visible_region_config(read(source_config))
    output.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[3]
    sources = [p for p in (repo / 'src/nav_data').rglob('*.py') if '__pycache__' not in p.parts]
    hashes = {}
    for source in sources:
        relative = source.relative_to(repo)
        target = output / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[str(relative)] = digest(target)
    write(output / 'config.json', config)
    write(output / 'source-provenance.json', {
        'owner': config['owner'], 'source_config': str(source_config.resolve()),
        'source_config_sha256': digest(source_config), 'config_sha256': digest(output / 'config.json'),
        'source_sha256': hashes, 'status': 'prepared_not_launched',
        'model_requests': 0, 'movement_commands': 0, 'training_exports': 0})
    return {'config': str(output / 'config.json'), 'owner': config['owner'],
            'cases': len(config['cases'])}


def collect_short(root):
    """Run one prepared short-instruction root against the sim-engine collection service."""


    root = Path(root)
    config = read(root / 'config.json')
    validate_distance_limits(config.get('min_local_geodesic_m', 0.0), config.get('max_local_geodesic_m'))
    frozen = read(root / 'source-provenance.json')
    if frozen['owner'] != config['owner'] or frozen['config_sha256'] != digest(root / 'config.json'):
        raise ValueError('short-instruction configuration provenance mismatch')
    repo = Path(__file__).resolve().parents[3]
    for relative, expected in frozen['source_sha256'].items():
        if digest(root / 'source' / relative) != expected or digest(repo / relative) != expected:
            raise ValueError('nav-data source changed since prepare: ' + relative)
    if 'collection_service' not in config:
        raise ValueError('short collection requires an explicit sim-engine collection_service')
    backend = copy.deepcopy(config['backend_manifest'])
    backend['config'].update(short_concurrency(config))
    scene_root = Path(backend['runtime']['settings']['simulation']['scenes_root']).resolve()
    candidates, tasks = [], []
    for item in config['cases']:
        require_train(config, item)
        scene = (scene_root / item['episode']['scene_id']).resolve()
        if not scene.is_relative_to(scene_root):
            raise ValueError('scene escapes frozen asset root')
        candidates.append({'id': item['id'], 'task_id': item['id'], 'scene': str(scene), 'case': item})
        tasks.append({'id': item['id'], 'scene': str(scene), 'instruction': ''})
    manifest = dict(backend, owner=config['owner'], candidates=candidates, tasks=tasks,
                    workflow_config={k: v for k, v in config.items() if k != 'cases'})
    return run_remote(manifest, root, 'short_instructions', config['collection_service'],
                      prepared_output=True)
