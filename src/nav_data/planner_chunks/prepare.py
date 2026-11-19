"""Read-only source preparation: merge, freeze, group by scene; never import Habitat."""
import copy
import hashlib
import math
import random
import uuid
from pathlib import Path

from nav_data.collection.pool import validate_concurrency
from nav_data.planner_chunks.merging import merge_short_chunks
from nav_data.records import digest, read, write


def code_identity():
    """Hash of every nav-data module a frozen R2R manifest depends on at run time."""
    package = Path(__file__).resolve().parents[1]
    return {str(p): digest(p) for p in sorted(package.rglob('*.py')) if '__pycache__' not in p.parts}


def snapshot_identity(root):
    """Hash a pinned sim-engine source tree; collection itself runs inside that service."""
    root = Path(root).resolve()
    if not (root / 'sim_engine/__init__.py').is_file():
        raise ValueError('snapshot_root must be a pinned sim-engine source tree (contains sim_engine/)')
    return root, {str(p): digest(p) for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def validate_config(config):
    if config.get('split') != 'train':
        raise ValueError('only train collection inputs are allowed')
    for key in ['max_tasks', 'max_candidates', 'draws_per_mode', 'ready_queue_size',
                'candidate_timeout_s', 'request_timeout_s', 'max_steps']:
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    for key, limit in [('start_radius_m', 1), ('yaw_degrees', 90), ('short_distance_m', 10)]:
        if type(config.get(key)) not in (int, float) or not math.isfinite(config[key]) or not 0 < config[key] <= limit:
            raise ValueError(f'invalid {key}')
    if type(config.get('seed')) is not int:
        raise ValueError('integer seed required')
    reserve = config.get('minimum_free_disk_bytes', 0)
    if type(reserve) is not int or reserve < 0:
        raise ValueError('minimum_free_disk_bytes must be a nonnegative integer')
    guide_radius = config.get('guide_adjustment_radius_m', .25)
    if type(guide_radius) not in (int, float) or not math.isfinite(guide_radius) or not 0 < guide_radius <= .5:
        raise ValueError('guide_adjustment_radius_m must be in (0, .5]')
    for key, default, low, high in [('minimum_planning_clearance_m', .05, .05, .15),
                                    ('endpoint_adjustment_radius_m', .15, 0., .3),
                                    ('expert_goal_radius_m', .08, .02, .25),
                                    ('goal_tolerance_m', .3, .1, .5)]:
        value=config.get(key,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'invalid {key}')
    if config.get('endpoint_adjustment_radius_m',.15)+config.get('expert_goal_radius_m',.08) >= config.get('goal_tolerance_m',.3):
        raise ValueError('endpoint adjustment plus controller stop radius must fit within goal tolerance')
    attempts = config.get('pose_sampling_attempts', 32)
    if type(attempts) is not int or not 1 <= attempts <= 128:
        raise ValueError('pose_sampling_attempts must be an integer in [1, 128]')
    validate_concurrency(config)
    allowed = {'split', 'max_tasks', 'max_candidates', 'draws_per_mode', 'ready_queue_size',
               'candidate_timeout_s', 'request_timeout_s', 'max_steps', 'start_radius_m',
               'yaw_degrees', 'short_distance_m', 'seed', 'simulation_workers', 'model_profiles',
               'source_collection', 'source_review', 'base_request', 'snapshot_root', 'pose_sampling_attempts', 'guide_adjustment_radius_m', 'minimum_free_disk_bytes', 'minimum_planning_clearance_m', 'endpoint_adjustment_radius_m', 'expert_goal_radius_m', 'goal_tolerance_m','collection_service'}
    if set(config) - allowed:
        raise ValueError('unknown configuration fields; secrets must not enter snapshots')


def perturbation(seed, candidate_id, radius, yaw_degrees):
    # A per-candidate stream; scheduling, exclusion and worker count cannot change a draw.
    value = int.from_bytes(hashlib.sha256(f'{seed}:{candidate_id}'.encode()).digest(), 'big')
    rng = random.Random(value)
    r = radius * math.sqrt(rng.random())
    theta = rng.uniform(-math.pi, math.pi)
    return {'offset': [r * math.cos(theta), 0., r * math.sin(theta)],
            'yaw_delta': rng.uniform(-math.radians(yaw_degrees), math.radians(yaw_degrees))}


def prepare(config, output):
    validate_config(config)
    config = copy.deepcopy(config)
    config.setdefault('pose_sampling_attempts', 32)
    config.setdefault('guide_adjustment_radius_m', .25)
    freeze_relaxed_defaults(config)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('prepare requires a new output directory')
    collection, review = Path(config['source_collection']), Path(config['source_review'])
    summary_path = review / 'summary.json'
    summary = read(summary_path)
    if summary.get('split') != 'train':
        raise ValueError('review source must explicitly be train')
    if summary.get('source_directory') and Path(summary['source_directory']).resolve() != collection.resolve():
        raise ValueError('review does not belong to the selected collection')
    base_path = Path(config['base_request'])
    base = read(base_path)
    if base.get('split') != 'train':
        raise ValueError('base simulation request must explicitly be train')
    # Freeze only simulation settings. Old deployment credentials and model fields are excluded.
    runtime = {k: base[k] for k in ('settings', 'camera', 'clearance_profile')}
    runtime['settings'] = {'simulation': copy.deepcopy(base['settings']['simulation']),
                           'policy': {'model': '/unused/no-model-loaded'}}
    snapshots, hashes = snapshot_identity(config['snapshot_root'])
    hashes.update({str(p.resolve()): digest(p) for p in (summary_path, base_path)})
    tasks, candidates, groups_audit = [], [], []
    scenes_root = Path(runtime['settings']['simulation']['scenes_root']).resolve()
    for report in sorted(summary['reports'], key=lambda r: r['episode']):
        if report['status'] != 'collected_pending_review' or report.get('collision_steps') != 0:
            continue
        eid = report['episode']
        if type(eid) is not int or eid < 0:
            raise ValueError('invalid parent episode ID')
        folder = collection / 'episodes' / str(eid)
        paths = [folder / 'source.json', folder / 'world_states.jsonl', folder / 'prepared_path.json',
                 review / 'episodes' / str(eid) / 'chunks.json']
        hashes.update({str(p.resolve()): digest(p) for p in paths})
        ep, prepared, chunks = read(paths[0]), read(paths[2]), read(paths[3])
        import json
        rows = [json.loads(line) for line in paths[1].read_text().splitlines()]
        scene = (scenes_root / ep['scene_id']).resolve()
        if not scene.is_relative_to(scenes_root):
            raise ValueError('scene escapes frozen asset root')
        for asset in (scene, scene.with_suffix('.navmesh')):
            if str(asset) not in hashes:
                hashes[str(asset)] = digest(asset)
        for chunk in chunks:
            a, b = chunk['start_frame'], chunk['end_frame']
            if not 0 <= a <= b < len(rows):
                raise ValueError('chunk frame interval outside source')
            chunk['distance_m'] = sum(math.dist(x['world_position'], y['world_position'])
                                      for x, y in zip(rows[a:b], rows[a+1:b+1]))
        for group_index, group in enumerate(merge_short_chunks(chunks, config['short_distance_m'])):
            groups_audit.append(dict(group, parent_episode=eid, group_index=group_index))
            if len(tasks) >= config['max_tasks']:
                continue
            start = chunks[group['chunk_ids'][0]]['start_frame']
            end = chunks[group['chunk_ids'][-1]]['end_frame']
            if group['distance_m'] <= .05 or end <= start:
                continue  # Retained in grouping audit, not a movement candidate.
            subset = [{'world_position': row['world_position'], 'base_pose_se2': row['base_pose_se2'],
                       'source_frame': start+i} for i, row in enumerate(rows[start:end+1])]
            distances = [0.]
            for a, b in zip(subset, subset[1:]):
                distances.append(distances[-1] + math.dist(a['world_position'], b['world_position']))
            midpoint = min(range(len(subset)), key=lambda i: abs(distances[i] - distances[-1]/2))
            task_id = f'ep{eid}-group{group_index}'
            goal_view = chunks[group['chunk_ids'][-1]]['source_interval'][1]
            task = {'id': task_id, 'parent_episode': eid, 'scene': str(scene),
                    'instruction': group['instruction'], 'chunk_ids': group['chunk_ids'],
                    'rows': subset, 'goal_position': prepared['world_points'][prepared['anchor_indices'][goal_view-1]],
                    'source_frame_interval': [start, end]}
            tasks.append(task)
            for mode, index in [('start', 0), ('mid', midpoint)]:
                for draw in range(config['draws_per_mode']):
                    cid = f'{task_id}-{mode}-{draw}'
                    candidates.append({'id': cid, 'task_id': task_id, 'scene': str(scene), 'mode': mode,
                                       'source_frame': index, **perturbation(config['seed'], cid,
                                                config['start_radius_m'], config['yaw_degrees'])})
    if not candidates or len(candidates) > config['max_candidates']:
        raise ValueError('empty selection or candidate budget exceeded; adjust explicit limits')
    if len({c['id'] for c in candidates}) != len(candidates):
        raise ValueError('duplicate source episode or candidate identity')
    code_hashes = code_identity()
    manifest = {'version': 1, 'owner': uuid.uuid4().hex, 'config': config, 'runtime': runtime,
                'snapshot_root': str(snapshots), 'tasks': tasks, 'candidates': candidates,
                'grouping': groups_audit, 'source_sha256': hashes, 'code_sha256': code_hashes,
                'admission': 'audit_only', 'sampling': 'sha256-per-candidate-bounded-geometric-selection-v2',
                'reference_policy': 'ordered-local-soft-guides-fixed-goal-v1'}
    output.mkdir(parents=True)
    write(output / 'manifest.json', manifest)
    write(output / 'manifest.sha256.json', {'sha256': digest(output / 'manifest.json')})
    return manifest


def verify_manifest(root):
    root = Path(root)
    if digest(root / 'manifest.json') != read(root / 'manifest.sha256.json')['sha256']:
        raise ValueError('frozen manifest changed')
    manifest = read(root / 'manifest.json')
    validate_config(manifest['config'])
    for path, expected in {**manifest['source_sha256'], **manifest['code_sha256']}.items():
        if digest(path) != expected:
            raise ValueError(f'frozen input changed: {path}')
    return hydrate_manifest(manifest)


def hydrate_manifest(manifest):
    if 'task_shards' not in manifest:
        return manifest
    tasks, candidates, grouping = [], [], []
    for shard in manifest['task_shards']:
        if str(Path(shard).resolve()) not in manifest['source_sha256']:
            raise ValueError('unfrozen task shard')
        value = read(shard)
        tasks.extend(value['tasks']); candidates.extend(value['candidates']); grouping.extend(value['grouping'])
    if len(tasks) != manifest['task_count'] or len(candidates) != manifest['candidate_count']:
        raise ValueError('task shard counts do not match frozen manifest')
    return dict(manifest, tasks=tasks, candidates=candidates, grouping=grouping)


def freeze_relaxed_defaults(config):
    for key,value in {'minimum_planning_clearance_m':.05,'endpoint_adjustment_radius_m':.15,
                      'expert_goal_radius_m':.08,'goal_tolerance_m':.3}.items():
        config.setdefault(key,value)
