"""Offline R2R chunk preparation: frozen inputs, train-only sources and manifest integrity."""
import json
from pathlib import Path

import pytest

from nav_data.planner_chunks.collect import collect_r2r
from nav_data.planner_chunks.prepare import perturbation, prepare, validate_config, verify_manifest
from nav_data.records import write


def source_config(tmp_path):
    collection, review, snapshots, scenes = [tmp_path / n for n in ('collection', 'review', 'snapshot', 'scenes')]
    for p in (collection, review, snapshots / 'sim_engine', scenes):
        p.mkdir(parents=True)
    (snapshots / 'sim_engine/__init__.py').write_text('# pinned sim-engine source tree\n')
    for suffix in ('glb', 'navmesh'):
        (scenes / ('room.' + suffix)).write_bytes(b'fake asset')
    write(review / 'summary.json', {'split': 'train', 'reports': [
        {'episode': 1, 'status': 'collected_pending_review', 'collision_steps': 0}]})
    write(collection / 'episodes/1/source.json', {'scene_id': 'room.glb'})
    states = collection / 'episodes/1/world_states.jsonl'
    states.write_text('\n'.join(json.dumps({'world_position': [i, 0, 0], 'base_pose_se2': [0, 0, 0]})
                                for i in range(4)))
    write(collection / 'episodes/1/prepared_path.json',
          {'world_points': [[0, 0, 0], [3, 0, 0]], 'anchor_indices': [0, 1]})
    write(review / 'episodes/1/chunks.json', [{'chunk_id': 0, 'text': 'Enter the room.', 'start_frame': 0,
                                                 'end_frame': 3, 'source_interval': [1, 2]}])
    base = tmp_path / 'base.json'
    write(base, {'split': 'train', 'settings': {'simulation': {'scenes_root': str(scenes),
               'datasets': {'train_pilot': {}}}}, 'camera': {}, 'clearance_profile': {}})
    return {'split': 'train', 'source_collection': str(collection), 'source_review': str(review),
            'snapshot_root': str(snapshots), 'base_request': str(base), 'seed': 1, 'short_distance_m': 1.,
            'start_radius_m': .5, 'yaw_degrees': 45, 'max_tasks': 1, 'max_candidates': 4,
            'draws_per_mode': 2, 'ready_queue_size': 2, 'candidate_timeout_s': 30, 'request_timeout_s': 2,
            'max_steps': 400, 'simulation_workers': [{'id': 's0', 'gpu': 0}],
            'model_profiles': [{'id': 'm0', 'model': 'fake',
                                'endpoint': 'http://localhost:18110/v1/chat/completions', 'slots': 1}]}


def test_sampling_independent_of_scheduling():
    a = perturbation(123, 'a', .5, 45)
    perturbation(123, 'b', .5, 45)
    assert perturbation(123, 'a', .5, 45) == a
    assert sum(x * x for x in a['offset']) <= .25
    assert abs(a['yaw_delta']) <= 3.1416 / 4


def test_prepare_is_offline_and_detects_source_or_manifest_change(tmp_path):
    config = source_config(tmp_path)
    out = tmp_path / 'prepared'
    m = prepare(config, out)
    assert len(m['candidates']) == 4
    assert not (out / 'attempt').exists()
    assert verify_manifest(out)['owner'] == m['owner']
    assert any(path.endswith('planner_chunks/collect.py') for path in m['code_sha256'])
    (Path(config['source_collection']) / 'episodes/1/source.json').write_text('{}')
    with pytest.raises(ValueError, match='frozen input changed'):
        verify_manifest(out)
    (out / 'manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='frozen manifest changed'):
        verify_manifest(out)


def test_prepare_requires_a_sim_engine_source_snapshot(tmp_path):
    config = source_config(tmp_path)
    (Path(config['snapshot_root']) / 'sim_engine/__init__.py').unlink()
    with pytest.raises(ValueError, match='sim-engine source tree'):
        prepare(config, tmp_path / 'prepared')


def test_prepare_refuses_evaluation_sources(tmp_path):
    config = source_config(tmp_path)
    config['split'] = 'val_unseen'
    with pytest.raises(ValueError):
        prepare(config, tmp_path / 'prepared')
    assert not (tmp_path / 'prepared').exists()


def test_sampling_budget_frozen_and_validated(tmp_path):
    config = source_config(tmp_path)
    assert prepare(config, tmp_path / 'prepared')['config']['pose_sampling_attempts'] == 32
    for bad in [0, 129, True, 1.5]:
        with pytest.raises(ValueError, match='pose_sampling_attempts'):
            validate_config(dict(config, pose_sampling_attempts=bad))


def test_relaxed_policy_defaults_are_frozen_and_cannot_exceed_goal_region(tmp_path):
    config = source_config(tmp_path)
    prepared = prepare(config, tmp_path / 'prepared')['config']
    assert prepared['minimum_planning_clearance_m'] == .05
    assert prepared['endpoint_adjustment_radius_m'] == .15
    assert prepared['expert_goal_radius_m'] == .08
    assert prepared['goal_tolerance_m'] == .3
    with pytest.raises(ValueError, match='minimum_planning_clearance_m'):
        validate_config(dict(config, minimum_planning_clearance_m=.01))
    with pytest.raises(ValueError, match='fit within goal tolerance'):
        validate_config(dict(config, endpoint_adjustment_radius_m=.25))


def test_collect_requires_an_explicit_collection_service(tmp_path):
    out = tmp_path / 'prepared'
    prepare(source_config(tmp_path), out)
    with pytest.raises(ValueError, match='collection_service'):
        collect_r2r(out)
    assert not (out / 'attempt').exists()
