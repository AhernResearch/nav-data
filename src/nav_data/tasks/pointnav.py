"""Private PointNav labels for visible-region recordings; no model or movement calls.

The intended goal is fixed before execution. The actual stop is an outcome, never
a replacement goal. Audit bundles require downstream review before training use.
"""
import json
import math
import os
from pathlib import Path

import numpy as np

from nav_data.records import read, write


def vector(value, size):
    result = np.asarray(value, dtype=float)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError('invalid PointNav coordinate or quaternion')
    return result


def rotation(value):
    result = vector(value, 4)
    norm = np.linalg.norm(result)
    if not math.isclose(norm, 1., abs_tol=1e-5):
        raise ValueError('PointNav quaternion must be unit xyzw')
    return result / norm


def require_train(config, item):
    """Admit legacy train inputs or explicitly registered GS development inputs."""
    backend = config['backend_manifest']
    split = backend['config']['split']
    episode = item['episode']
    simulation = backend.get('runtime', {}).get('settings', {}).get('simulation', {})
    dataset = simulation.get('datasets', {}).get(episode.get('_dataset'), {})
    if split == 'train' and episode.get('_dataset') == 'train_pilot':
        if dataset.get('task') == 'development':
            raise ValueError('development scenes cannot be labeled as train source')
        return
    if (split == 'development' and simulation.get('backend') == 'habitat_gs'
            and dataset.get('task') == 'development'
            and episode.get('scene_id') in dataset.get('scenes', {})
            and config.get('collection_service', {}).get('dataset') == episode.get('_dataset')):
        return
    raise ValueError('PointNav collection requires train source or registered GS development source')


def target_record(config, item, task, grounded, goal, *, expert_radius_m=.08):
    """Build an intended-goal record without consulting an executed endpoint."""
    require_train(config, item)
    goal = vector(goal, 3)
    if not np.allclose(goal, vector(grounded['navmesh_point'], 3), atol=1e-7, rtol=0):
        raise ValueError('PointNav target differs from selected NavMesh point')
    radius = config['backend_manifest']['config']['goal_tolerance_m']
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
           for v in (radius, expert_radius_m)):
        raise ValueError('invalid PointNav success or controller radius')
    episode = item['episode']
    start = vector(episode['start_position'], 3).tolist()
    rotation(episode['start_rotation'])
    scene = Path(episode['scene_id'])
    if scene.is_absolute() or '..' in scene.parts:
        raise ValueError('PointNav scene must be dataset relative')
    return {
        'schema_version': 1, 'task': 'PointNav', 'owner': config['owner'],
        'world_frame': 'Habitat_XYZ_Y_up', 'quaternion_order': 'xyzw', 'length_unit': 'meters',
        'episode': {'episode_id': item['id'], 'scene_id': str(scene),
                    'start_position': start, 'start_rotation': list(episode['start_rotation']),
                    'goals': [{'position': goal.tolist(), 'radius': radius}]},
        'success_criteria': {'distance': 'navmesh_geodesic', 'require_expert_stop': True,
                             'goal_tolerance_m': radius, 'expert_controller_radius_m': expert_radius_m},
        'target_source': {'kind': 'system_selected_visible_navmesh_point',
                          'target_view': task['target_view'], 'bbox': task.get('bbox'),
                          'pixel_xy': grounded.get('pixel_xy'),
                          'surface_world_point': grounded.get('world_point'),
                          'depth_m': grounded.get('depth_m')},
        'source_split': config['backend_manifest']['config']['split'],
        'training_admission': 'pending_review',
        'semantic_admission': 'pending_review', 'visibility': 'private_geometry',
    }


def frame_goal(row, goal):
    """Current-agent goal: forward=-Z, left=-X, up=Y, bearing positive left.

    This is a changing per-frame goal, not a frozen start-frame observation.
    Planar distance is Euclidean; it is not a geodesic success measurement.
    """
    delta = vector(goal, 3) - vector(row['world_position'], 3)
    q = rotation(row['world_rotation_xyzw'])
    xyz, w = q[:3], q[3]
    local = delta - 2*w*np.cross(xyz, delta) + 2*np.cross(xyz, np.cross(xyz, delta))
    forward, left, up = -local[2], -local[0], local[1]
    return {'frame': row['frame'], 'control_time_s': row['control_time_s'],
            'goal_base_forward_left_up_m': [float(forward), float(left), float(up)],
            'goal_distance_planar_m': float(math.hypot(forward, left)),
            'goal_bearing_left_rad': float(math.atan2(left, forward))}


def outcome_record(target, rows, geometry, terminal, recording_complete, region_terminal):
    if not rows:
        raise ValueError('PointNav outcome requires an actual observed state')
    goal = target['episode']['goals'][0]['position']
    final = rows[-1]
    vector(final['world_position'], 3)
    rotation(final['world_rotation_xyzw'])
    distance = geometry['final_distance_m']
    if not math.isfinite(distance) or distance < 0:
        raise ValueError('invalid actual goal distance')
    if math.dist(final['world_position'], goal) > distance + .01:
        raise ValueError('geodesic goal error contradicts observed endpoint')
    complete = (geometry['passed'] and geometry['termination'] == 'expert_stop'
                and geometry['collision_steps'] == 0 and terminal['receipt_stop']
                and terminal['termination'] == 'expert_stop' and terminal['frame'] == final['frame']
                and distance <= target['success_criteria']['goal_tolerance_m']
                and recording_complete
                and (region_terminal is None or region_terminal['inside_original_bbox']))
    return {'schema_version': 1, 'episode_id': target['episode']['episode_id'],
            'status': 'completed_audit' if complete else 'failed_or_uncertain',
            'intended_goal_position': goal,
            'last_observed_pose': {'position': final['world_position'],
                                   'rotation_xyzw': final['world_rotation_xyzw'], 'frame': final['frame']},
            'receipt_stop': terminal['receipt_stop'], 'termination': terminal['termination'],
            'final_geodesic_distance_m': distance,
            'final_euclidean_distance_m': math.dist(final['world_position'], goal),
            'collision_steps': geometry['collision_steps'], 'recording_complete': recording_complete,
            'inside_original_bbox': None if region_terminal is None else region_terminal['inside_original_bbox'],
            'training_admission': 'pending_review'}


def read_rows(folder):
    folder = Path(folder)
    if (folder/'meta.json').exists():
        from nav_data.release.compact import Episode
        return Episode(folder/'meta.json').data['world_states']
    if not folder.exists() and (folder.parent/'episodes.sqlite3').exists():
        from nav_data.release.compact import open_episode
        return open_episode(folder.parent, folder.name).data['world_states']
    return [json.loads(line) for line in (Path(folder)/'world_states.jsonl').read_text().splitlines()]


def write_frames(path, rows, target):
    goal = target['episode']['goals'][0]['position']
    with Path(path).open('x') as stream:
        stream.writelines(json.dumps(frame_goal(row, goal), allow_nan=False) + '\n' for row in rows)
        stream.flush()
        os.fsync(stream.fileno())


def save_outcome(folder, target, geometry, recording_complete, region_terminal):
    folder = Path(folder)
    rows = read_rows(folder)
    outcome = outcome_record(target, rows, geometry, read(folder/'terminal.json'),
                             recording_complete, region_terminal)
    write(folder/'pointnav-outcome.json', outcome)
    if recording_complete:
        write_frames(folder/'pointnav-frames.jsonl', rows, target)
    return outcome
