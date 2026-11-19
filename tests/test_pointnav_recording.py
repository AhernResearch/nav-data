"""PointNav goal identity, coordinate signs and immutable offline derivation."""
import copy
import json
import math

import pytest
from recordings import recording

from nav_data.records import digest, read, write
from nav_data.tasks.pointnav import (
    frame_goal,
    outcome_record,
    save_outcome,
    target_record,
)


def inputs():
    item = {'id': 'visible-box', 'episode': {'_dataset': 'train_pilot',
            'scene_id': 'mp3d/scene/scene.glb', 'start_position': [0, 0, 0],
            'start_rotation': [0, 0, 0, 1]}}
    config = {'owner': 'source-owner', 'backend_manifest': {'config': {'split': 'train',
              'goal_tolerance_m': .12}}, 'target_mode': 'box', 'cases': [item]}
    task = {'target_view': 'front', 'bbox': [300, 400, 600, 800]}
    grounded = {'navmesh_point': [0, 0, -1.15], 'world_point': [0, .01, -1.15],
                'pixel_xy': [2, 1], 'depth_m': 1.15}
    return config, item, task, grounded


def test_intended_goal_is_distinct_from_stop_and_uses_actual_success_tolerance():
    config, item, task, grounded = inputs()
    target = target_record(config, item, task, grounded, grounded['navmesh_point'])
    rows = [{'frame': 1, 'world_position': [0, 0, -1.1], 'world_rotation_xyzw': [0, 0, 0, 1]}]
    geometry = {'passed': True, 'termination': 'expert_stop', 'collision_steps': 0, 'final_distance_m': .05}
    terminal = {'frame': 1, 'receipt_stop': True, 'termination': 'expert_stop'}
    outcome = outcome_record(target, rows, geometry, terminal, True, {'inside_original_bbox': True})
    assert target['episode']['goals'] == [{'position': [0, 0, -1.15], 'radius': .12}]
    assert target['success_criteria']['expert_controller_radius_m'] == .08
    assert outcome['last_observed_pose']['position'] == [0, 0, -1.1]
    assert outcome['intended_goal_position'] == [0, 0, -1.15]
    assert outcome['status'] == 'completed_audit'
    assert target['training_admission'] == outcome['training_admission'] == 'pending_review'
    for recording_complete, stop, region in [(False, True, True), (True, False, True), (True, True, False)]:
        terminal['receipt_stop'] = stop
        result = outcome_record(target, rows, geometry, terminal, recording_complete,
                                {'inside_original_bbox': region})
        assert result['status'] == 'failed_or_uncertain'


@pytest.mark.parametrize('yaw,goal,expected', [
    (0, [0, 0, -2], [2, 0, 0]), (0, [-2, 0, 0], [0, 2, 0]),
    (0, [2, 0, 0], [0, -2, 0]), (math.pi/2, [-2, 0, 0], [2, 0, 0]),
    (math.pi/2, [0, .3, 2], [0, 2, .3]),
])
def test_per_frame_goal_matches_forward_left_up_and_current_yaw(yaw, goal, expected):
    row = {'frame': 7, 'control_time_s': 1.75, 'world_position': [10, 3, 8],
           'world_rotation_xyzw': [0, math.sin(yaw/2), 0, math.cos(yaw/2)]}
    result = frame_goal(row, [a+b for a, b in zip(row['world_position'], goal)])
    assert result['goal_base_forward_left_up_m'] == pytest.approx(expected, abs=1e-8)
    assert result['goal_distance_planar_m'] == pytest.approx(2)
    assert result['goal_bearing_left_rad'] == pytest.approx(math.atan2(expected[1], expected[0]), abs=1e-8)
    assert result['frame'] == 7


def test_pointnav_rejects_eval_source_nonfinite_and_mismatched_targets():
    config, item, task, grounded = inputs()
    bad = copy.deepcopy(item); bad['episode']['_dataset'] = 'val_unseen'
    with pytest.raises(ValueError, match='train source'):
        target_record(config, bad, task, grounded, grounded['navmesh_point'])
    config['backend_manifest']['config']['split'] = 'val'
    with pytest.raises(ValueError, match='train source'):
        target_record(config, item, task, grounded, grounded['navmesh_point'])
    config['backend_manifest']['config']['split'] = 'train'
    with pytest.raises(ValueError, match='selected NavMesh'):
        target_record(config, item, task, grounded, [0, 0, -1.1])
    with pytest.raises(ValueError, match='coordinate'):
        target_record(config, item, task, grounded, [0, float('nan'), 0])
    item['episode']['start_rotation'] = [0, 0, 0, 2]
    with pytest.raises(ValueError, match='unit xyzw'):
        target_record(config, item, task, grounded, grounded['navmesh_point'])


def source_run(tmp_path, *, n=12):
    source = tmp_path/'run'; source.mkdir()
    config, item, task, grounded = inputs()
    # Keep the intended goal near the actual terminal state for either recording length.
    grounded['navmesh_point'][2] = -(n-1)*.1 - .05
    grounded['world_point'][2] = grounded['navmesh_point'][2]
    folder = source/item['id']; folder.mkdir(); recording(folder, n)
    prepared_episode = dict(item['episode'], goals=[{'position': grounded['navmesh_point'], 'radius': .08}])
    write(folder/'private-geometry.json', {'episode': prepared_episode,
          'requested_goal_position': grounded['navmesh_point']})
    geometry = {'passed': True, 'termination': 'expert_stop', 'collision_steps': 0,
                'final_distance_m': .05, 'steps': n-1, 'source': 'expert_not_vla'}
    write(folder/'geometry-result.json', {'status': 'geometry_complete', 'geometry': geometry})
    write(folder/'result.json', {'status': 'completed_audit', 'geometry': geometry})
    write(folder/'proposed-task.json', task); write(folder/'grounded-target.json', grounded)
    terminal = read(folder/'terminal.json')
    terminal.update(requested_goal_distance_m=.05, goal_tolerance_m=.12)
    (folder/'terminal.json').write_text(json.dumps(terminal))
    write(folder/'region-terminal-check.json', {'inside_original_bbox': True})
    failed = copy.deepcopy(item); failed['id'] = 'excluded'
    config['cases'].append(failed)
    write(source/'excluded/result.json', {'status': 'excluded_before_movement', 'error': 'no visible ground'})
    write(source/'config.json', config)
    write(source/'source-provenance.json', {'owner': config['owner'],
          'config_sha256': digest(source/'config.json'), 'source_sha256': {}})
    write(source/'summary.json', {'owner': config['owner']})
    write(source/'semantic-review-full.json', {'records': [{'id': item['id'], 'verdict': 'direction_conflict'}]})
    return source


def test_live_sidecars_keep_the_executed_stop_separate_and_are_create_only(tmp_path):
    source = source_run(tmp_path); folder = source/'visible-box'
    config = read(source/'config.json'); item = config['cases'][0]
    grounded = read(folder/'grounded-target.json')
    target = target_record(config, item, read(folder/'proposed-task.json'), grounded, grounded['navmesh_point'])
    write(folder/'pointnav-target.json', target)
    geometry = read(folder/'geometry-result.json')['geometry']
    outcome = save_outcome(folder, target, geometry, True, {'inside_original_bbox': True})
    assert outcome['intended_goal_position'] != outcome['last_observed_pose']['position']
    with pytest.raises(FileExistsError): save_outcome(folder, target, geometry, True, None)
