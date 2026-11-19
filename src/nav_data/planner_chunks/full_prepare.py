"""Train-only FG/continuous-reference join and scene-parallel geometric preparation.

Reference rows here are NavMesh geometry, not collected expert states. Actual
expert execution and model annotation remain in the ordinary pose pipeline.
"""
import ast
import copy
import gzip
import json
import math
import multiprocessing as mp
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from nav_data.collection.client import measure_reference
from nav_data.planner_chunks.merging import merge_short_chunks
from nav_data.planner_chunks.prepare import (
    code_identity,
    freeze_relaxed_defaults,
    perturbation,
    snapshot_identity,
    validate_config,
)
from nav_data.records import digest, read, write

FG_TRAIN_SHA = '703837ee87a14d1b1fcb7d5bd6ce261faacc82e0d911a16396792cfa53d54fbc'
CE_TRAIN_SHA = '108d6ae183dcb5233d6d5337a599c78a4cb062523f27bd7b18e8b9e3e6cb7895'


def normalize(text):
    return ' '.join(text.split()).casefold()


def join_sources(fg, episodes):
    paths = {r['path_id']: r for r in fg}
    if len(paths) != len(fg):
        raise ValueError('duplicate FG path IDs')
    joined, exclusions, covered = [], [], set()
    for ep in episodes:
        row = paths.get(ep['trajectory_id'])
        if row is None:
            exclusions.append({'episode': ep['episode_id'], 'reason': 'no_fg_train_path'})
            continue
        matches = [i for i, text in enumerate(row['instructions'])
                   if normalize(text) == normalize(ep['instruction']['instruction_text'])]
        if len(matches) != 1:
            exclusions.append({'episode': ep['episode_id'], 'reason': 'instruction_join_not_unique'})
            continue
        i = matches[0]
        if row['scan'] != Path(ep['scene_id']).stem or len(row['path']) != len(ep['reference_path']):
            exclusions.append({'episode': ep['episode_id'], 'reason': 'scene_or_anchor_count_mismatch'})
            continue
        key = (row['path_id'], i)
        if key in covered:
            exclusions.append({'episode': ep['episode_id'], 'reason': 'duplicate_instruction_route'})
            continue
        chunks = ast.literal_eval(row['new_instructions']) if isinstance(row['new_instructions'], str) else row['new_instructions']
        if i >= len(chunks) or i >= len(row['chunk_view']) or len(chunks[i]) != len(row['chunk_view'][i]):
            exclusions.append({'episode': ep['episode_id'], 'reason': 'missing_or_mismatched_fg_chunk_annotation'})
            continue
        joined.append(dict(ep, fg={'path_id': row['path_id'], 'instruction_index': i,
                      'chunks': chunks[i], 'chunk_view': row['chunk_view'][i]}))
        covered.add(key)
    missing = [{'path_id': row['path_id'], 'instruction_index': i, 'reason': 'no_unique_continuous_train_reference'}
               for row in fg for i in range(len(row['instructions'])) if (row['path_id'], i) not in covered]
    return joined, exclusions, missing


def expand_shared_viewpoints(fg, joined):
    """Reuse only consistent coordinates of identical train scan/viewpoint IDs."""
    by_path = {row['path_id']: row for row in fg}
    positions, conflicts = {}, set()
    for ep in joined:
        row = by_path[ep['fg']['path_id']]
        for node, point in zip(row['path'], ep['reference_path']):
            key = (row['scan'], node)
            if key in positions and math.dist(positions[key], point) > .03:
                conflicts.add(key)
            else:
                positions.setdefault(key, point)
    covered = {(ep['fg']['path_id'], ep['fg']['instruction_index']) for ep in joined}
    extra, missing = [], []
    for row in fg:
        keys = [(row['scan'], node) for node in row['path']]
        absent = [node for scan, node in keys if (scan, node) not in positions or (scan, node) in conflicts]
        chunks = ast.literal_eval(row['new_instructions']) if isinstance(row['new_instructions'], str) else row['new_instructions']
        for i, text in enumerate(row['instructions']):
            if (row['path_id'], i) in covered:
                continue
            if i >= len(chunks) or i >= len(row['chunk_view']) or len(chunks[i]) != len(row['chunk_view'][i]):
                missing.append({'path_id': row['path_id'], 'instruction_index': i,
                                'reason': 'missing_or_mismatched_fg_chunk_annotation'})
                continue
            if absent:
                missing.append({'path_id': row['path_id'], 'instruction_index': i,
                                'reason': 'missing_or_inconsistent_train_viewpoint_coordinates', 'viewpoints': absent})
                continue
            points = [positions[key] for key in keys]
            yaw = -float(row['heading'])
            extra.append({'episode_id': f"fg{row['path_id']}i{i}", 'trajectory_id': row['path_id'],
                          'scene_id': f"mp3d/{row['scan']}/{row['scan']}.glb",
                          'start_position': points[0], 'start_rotation': [0, math.sin(yaw/2), 0, math.cos(yaw/2)],
                          'goals': [{'position': points[-1], 'radius': .3}], 'reference_path': points,
                          'instruction': {'instruction_text': text},
                          'fg': {'path_id': row['path_id'], 'instruction_index': i,
                                 'chunks': chunks[i], 'chunk_view': row['chunk_view'][i],
                                 'reference_origin': 'identical_train_scan_viewpoint_coordinates'}})
    return extra, missing


def sparse_reference(ep, shortest):
    """Measure original discrete route legs without generating dense guides."""
    anchors = ep['reference_path']
    legs = []
    for a, b in zip(anchors, anchors[1:]):
        segment = shortest(a, b)
        if not segment:
            raise ValueError('disconnected reference anchors')
        legs.append({'distance_m': sum(math.dist(x, y) for x, y in zip(segment, segment[1:])),
                     'initial_yaw': next((math.atan2(-(y[0]-x[0]), -(y[2]-x[2]))
                                          for x, y in zip(segment, segment[1:])
                                          if math.hypot(y[0]-x[0], y[2]-x[2]) > 1e-8), None)})
    return legs


def tasks_for_episode(ep, legs, scene, config):
    chunks = []
    anchors = ep['reference_path']
    for cid, (words, bounds) in enumerate(zip(ep['fg']['chunks'], ep['fg']['chunk_view'])):
        a, b = bounds
        if not 1 <= a <= b <= len(anchors):
            raise ValueError('invalid FG reference interval')
        chunks.append({'chunk_id': cid, 'text': ' '.join(words),
                       'distance_m': sum(leg['distance_m'] for leg in legs[a-1:b-1]), 'source_interval': bounds})
    tasks, candidates, audit = [], [], []
    q = ep['start_rotation']
    original_yaw = math.atan2(2*q[3]*q[1], 1-2*q[1]*q[1])
    for index, group in enumerate(merge_short_chunks(chunks, config['short_distance_m'])):
        task_id = f"ep{ep['episode_id']}-group{index}"
        a, b = chunks[group['chunk_ids'][0]]['source_interval'][0]-1, chunks[group['chunk_ids'][-1]]['source_interval'][1]-1
        audit.append(dict(group, task_id=task_id, source_kind='ordered_sparse_train_reference', source_anchor_interval=[a, b]))
        if b <= a or group['distance_m'] <= .05:
            continue
        yaw = original_yaw if a == 0 else next((leg['initial_yaw'] for leg in legs[a:b] if leg['initial_yaw'] is not None), original_yaw)
        task = {'id': task_id, 'parent_episode': ep['episode_id'], 'scene': scene,
                'instruction': group['instruction'], 'chunk_ids': group['chunk_ids'],
                'reference_guides': anchors[a:b+1], 'source_start_yaw': yaw,
                'goal_position': anchors[b], 'source_anchor_interval': [a, b], 'fg': ep['fg'],
                'reference_source': 'ordered_sparse_train_reference_not_execution'}
        tasks.append(task)
        for mode, fraction in [('start', 0.), ('mid', .5)]:
            for draw in range(config['draws_per_mode']):
                cid = f'{task_id}-{mode}-{draw}'
                candidates.append({'id': cid, 'task_id': task_id, 'scene': scene, 'mode': mode,
                                   'reference_fraction': fraction,
                                   **perturbation(config['seed'], cid, config['start_radius_m'], config['yaw_degrees'])})
    return tasks, candidates, audit


def prepare_scene(scene, episodes, config, shard):
    cache, tasks, candidates, audit, exclusions = {}, [], [], [], []
    for ep in episodes:
        try:
            key = json.dumps([ep['reference_path'],ep['start_rotation']])
            if key not in cache:cache[key]=measure_reference(config,ep)
            t,c,g=tasks_for_episode(ep,cache[key],scene,config)
            tasks.extend(t);candidates.extend(c);audit.extend(g)
        except ValueError as error:
            exclusions.append({'episode':ep['episode_id'],'reason':str(error),'phase':'geometric_source_preparation'})
    write(shard,{'scene':scene,'tasks':tasks,'candidates':candidates,'grouping':audit,'exclusions':exclusions})
    return {'scene':scene,'tasks':len(tasks),'candidates':len(candidates),'excluded_episodes':len(exclusions)}


def prepare_full(config, episodes_path, fg_path, output):
    validate_config(config)
    if digest(episodes_path)!=CE_TRAIN_SHA or digest(fg_path)!=FG_TRAIN_SHA:
        raise ValueError('only the pinned train sources are accepted; evaluation data prohibited')
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    config=copy.deepcopy(config);config.setdefault('pose_sampling_attempts',32);config.setdefault('guide_adjustment_radius_m',.25)
    freeze_relaxed_defaults(config)
    base=read(config['base_request'])
    if base.get('split')!='train':raise ValueError('train runtime required')
    runtime={k:copy.deepcopy(base[k]) for k in ('settings','camera','clearance_profile')}
    runtime['settings']={'simulation':runtime['settings']['simulation'],'policy':{'model':'/unused/no-model-loaded'}}
    with gzip.open(episodes_path,'rt') as stream:episodes=json.load(stream)['episodes']
    fg=read(fg_path)
    joined,excluded,missing=join_sources(fg,episodes)
    direct_count=len(joined)
    extra,missing=expand_shared_viewpoints(fg,joined)
    joined.extend(extra)
    scenes=defaultdict(list);scene_root=Path(runtime['settings']['simulation']['scenes_root']).resolve()
    hashes={str(Path(p).resolve()):digest(p) for p in (episodes_path,fg_path,config['base_request'])}
    for ep in joined:
        scene=(scene_root/ep['scene_id']).resolve()
        if not scene.is_relative_to(scene_root):raise ValueError('scene path escapes assets')
        scenes[str(scene)].append(ep)
    inventory={'split':'train','fg_paths':len(fg),'fg_instruction_routes':sum(len(r['instructions']) for r in fg),
               'continuous_train_episodes':len(episodes),'joined_instruction_routes':len(joined),'direct_episode_matches':direct_count,
               'shared_train_viewpoint_matches':len(extra),'scene_count':len(scenes),
               'join_exclusions':excluded,'unmapped_fg_routes':missing,
               'reference_source':'NavMesh geometry, not historical expert execution', 'admission':'audit_only'}
    write(output/'inventory.json',inventory)
    for scene in scenes:
        for p in (Path(scene),Path(scene).with_suffix('.navmesh')):hashes[str(p)]=digest(p)
    snapshot,snapshot_hashes=snapshot_identity(config['snapshot_root'])
    hashes.update(snapshot_hashes)
    shards=[]
    with ProcessPoolExecutor(max_workers=len(config['simulation_workers']),mp_context=mp.get_context('spawn')) as pool:
        futures=[]
        for i,(scene,eps) in enumerate(sorted(scenes.items())):
            shard=output/'source-shards'/f'{i:03d}.json';shards.append(shard)
            futures.append(pool.submit(prepare_scene,scene,eps,config,shard))
        for future in as_completed(futures):print(json.dumps(future.result()),flush=True)
    tasks,candidates,groups,geometric_exclusions=[],[],[],[]
    for shard in shards:
        d=read(shard);tasks.extend(d['tasks']);candidates.extend(d['candidates']);groups.extend(d['grouping']);geometric_exclusions.extend(d['exclusions'])
        hashes[str(shard)]=digest(shard)
    if not tasks or len(tasks)>config['max_tasks'] or len(candidates)>config['max_candidates']:
        raise ValueError('full preparation exceeds explicit budget or has no tasks')
    if len({c['id'] for c in candidates})!=len(candidates):raise ValueError('duplicate candidates')
    manifest={'version':1,'owner':uuid.uuid4().hex,'config':config,'runtime':runtime,'snapshot_root':str(snapshot),
              'task_shards':[str(p) for p in shards], 'task_count':len(tasks), 'candidate_count':len(candidates),
              'source_sha256':hashes,
              'code_sha256':code_identity(),'admission':'audit_only',
              'sampling':'sha256-per-candidate-bounded-geometric-selection-v2',
              'reference_policy':'sparse-plan-comfort-fallback-goal-region-v3',
              'reference_source':'ordered_sparse_train_reference_not_execution'}
    write(output/'source-preparation-summary.json',{'tasks':len(tasks),'candidates':len(candidates),
          'geometric_exclusions':geometric_exclusions,'source_kind':manifest['reference_source']})
    write(output/'manifest.json',manifest);write(output/'manifest.sha256.json',{'sha256':digest(output/'manifest.json')})
    return {'prepared_tasks':len(tasks),'candidates':len(candidates),'owner':manifest['owner']}
