"""Raw recording contract and offline checks; no RVQ or training export during collection."""
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

from nav_data.records import digest, read
from nav_data.release.compact import Episode, open_episode


def calibration(camera, projections, near, far, dt=.25):
    width, height = camera['width'], camera['height']
    sensors = {}
    for key in ('rgb', 'depth'):
        p = projections[key]
        sensors[key] = {'projection_matrix': p, 'intrinsics_K': [
            [p[0][0]*width/2, 0., (width-1)/2],
            [0., p[1][1]*height/2, (height-1)/2], [0., 0., 1.]]}
    return {'schema_version': 2, 'camera': camera, 'projection_matrices': projections, 'sensors': sensors,
            'resolution_wh': [width, height], 'depth_near_far': [near, far],
            'depth_units': 'meters', 'depth_convention': 'axial_minus_camera_z',
            'depth_unknown_value': 0., 'control_dt_s': dt, 'video_fps': 1/dt,
            'views': ['front', 'right', 'back', 'left'],
            'world_frame': 'Habitat_XYZ_Y_up', 'base_frame': 'forward_left_ccw',
            'quaternion_order': 'xyzw', 'camera_forward': '-Z',
            'trajectory_source': 'actual_executed_states',
            'offline_conversion': {'format': 'LightNav', 'horizon': 10,
                'required_stages': ['review', 'local_future_SE2', 'pointing_visibility', 'RVQ_encoding'],
                'terminal_policy': 'expert_stop_receipt; visible_goal_and_admission_review_separate'}}


def local_future(rows, anchor, horizon=10):
    """Ten observed future poses in the anchor's forward-left-CCW coordinate frame."""
    if anchor+horizon >= len(rows):
        raise ValueError('not enough actual future states; do not invent or pad them')
    x, y, yaw = rows[anchor]['base_pose_se2']
    c, s = math.cos(yaw), math.sin(yaw)
    result = []
    for row in rows[anchor+1:anchor+horizon+1]:
        px, py, heading = row['base_pose_se2']
        dx, dy = px-x, py-y
        result.append([c*dx+s*dy, -s*dx+c*dy, (heading-yaw+math.pi)%(2*math.pi)-math.pi])
    return result


def validate_recording(folder):
    """Post-production conversion-readiness check, not training admission."""
    folder = Path(folder)
    compact = None
    if (folder/'meta.json').exists():
        compact = Episode(folder/'meta.json')
    elif not folder.exists() and (folder.parent/'episodes.sqlite3').exists():
        compact = open_episode(folder.parent, folder.name)
    if compact is None:
        camera = read(folder/'camera.json')
        rows = [json.loads(line) for line in (folder/'world_states.jsonl').read_text().splitlines()]
        commands = [json.loads(line) for line in (folder/'commands.jsonl').read_text().splitlines()]
        terminal = read(folder/'terminal.json')
    else:
        folder = compact.raw
        camera = compact.record('raw', 'camera.json')
        rows, commands = compact.data['world_states'], compact.data['commands']
        terminal = compact.record('raw', 'terminal.json')
    if camera.get('schema_version') != 2:
        raise ValueError('legacy recording needs explicit sensor-transform reconstruction '
                         'before conversion; this build has no legacy reconstructor')
    dt = camera['control_dt_s']
    if not math.isclose(dt, .25, abs_tol=1e-9):
        raise ValueError('LightNav 4Hz conversion requires explicit temporal resampling')
    if not terminal['receipt_stop'] or terminal['termination'] != 'expert_stop':
        raise ValueError('incomplete or failed execution cannot be converted as a completed trajectory')
    if terminal['frame'] != len(rows)-1 or len(commands) != 2*(len(rows)-1):
        raise ValueError('frame/command/terminal alignment mismatch')
    for key in ('rgb', 'depth'):
        p = np.asarray(camera['sensors'][key]['projection_matrix'])
        k = np.asarray(camera['sensors'][key]['intrinsics_K'])
        if p.shape != (4,4) or k.shape != (3,3) or not np.isfinite(p).all() or not np.isfinite(k).all():
            raise ValueError('invalid camera calibration')
    np.testing.assert_allclose(camera['projection_matrices']['rgb'], camera['projection_matrices']['depth'], atol=1e-6)
    for i, row in enumerate(rows):
        if row['frame'] != i or not math.isclose(row['control_time_s'], i*dt, abs_tol=1e-8) or row['collided']:
            raise ValueError('invalid frame timing or collision')
        if i == 0:
            if row['incoming_command'] is not None:
                raise ValueError('initial state cannot have an incoming command')
        else:
            before, after = commands[2*(i-1):2*i]
            if (before['phase'] != 'started' or after['phase'] != 'returned'
                    or before['step'] != i-1 or after['step'] != i-1 or after['collided']
                    or row['incoming_command'] != before['command']):
                raise ValueError('actual action/state alignment mismatch')
        world, pose = np.asarray(row['world_position']), np.asarray(row['base_pose_se2'])
        if world.shape != (3,) or pose.shape != (3,) or not np.isfinite(world).all() or not np.isfinite(pose).all():
            raise ValueError('invalid executed world/base pose')
        np.testing.assert_allclose(pose[:2], [-world[2], -world[0]], atol=1e-6)
        agent_q = np.asarray(row['world_rotation_xyzw'])
        if agent_q.shape != (4,) or not np.isfinite(agent_q).all() or not np.isclose(np.linalg.norm(agent_q), 1, atol=1e-5):
            raise ValueError('invalid executed agent orientation')
        for key in ('rgb', 'depth'):
            sensor = row['sensors'][key]
            q, pos = np.asarray(sensor['rotation_xyzw']), np.asarray(sensor['position'])
            if q.shape != (4,) or pos.shape != (3,) or not np.isfinite(pos).all() or not np.isfinite(q).all() or not np.isclose(np.linalg.norm(q),1,atol=1e-5):
                raise ValueError('invalid recorded sensor pose')
        np.testing.assert_allclose(row['sensors']['rgb']['position'], row['sensors']['depth']['position'], atol=1e-5)
        if abs(np.dot(row['sensors']['rgb']['rotation_xyzw'], row['sensors']['depth']['rotation_xyzw'])) < 1-1e-5:
            raise ValueError('RGB/depth sensor orientations differ')
        image, depth_file = folder/'rgb'/f'{i:05d}.jpg', folder/'depth'/f'{i:05d}.npz'
        if digest(image) != row['rgb_sha256'] or digest(depth_file) != row['depth_sha256']:
            raise ValueError('recorded media hash mismatch')
        with Image.open(image) as image_data:
            if list(image_data.size) != camera['resolution_wh']:
                raise ValueError('RGB/calibration size mismatch')
        with np.load(depth_file,allow_pickle=False) as data:
            depth = data['depth_m']
        if depth.shape != tuple(reversed(camera['resolution_wh'])) or depth.dtype != np.float32 or not np.isfinite(depth).all() or (depth < 0).any() or not (depth > 0).any():
            raise ValueError('invalid metric depth')
    return {'schema_version': 2, 'frames': len(rows), 'motion_windows': max(0,len(rows)-10),
            'control_dt_s': dt, 'horizon': 10, 'receipt_stop': True,
            'conversion_inputs_complete': True, 'sensor_pose_source': camera.get('sensor_pose_source', 'recorded_per_frame'),
            'training_admission': 'pending_review',
            'visible_goal_label': terminal['visible_goal_label'],
            'endpoint_review_required':terminal.get('endpoint_review_required',False),
            'goal_adjustment_m':terminal.get('goal_adjustment_m',0.),
            'first_motion_target': local_future(rows,0) if len(rows)>10 else None}
