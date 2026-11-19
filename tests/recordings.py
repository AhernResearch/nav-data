"""Synthetic recording fixture shared by the collection release tests."""

import json

import numpy as np
from PIL import Image

from nav_data.records import digest, write
from nav_data.tasks.recording import calibration


def recording(tmp_path, n=12):
    p=np.eye(4).tolist()
    camera=calibration({'width':4,'height':2}, {'rgb':p,'depth':p}, .1,1000.)
    write(tmp_path/'camera.json',camera)
    (tmp_path/'rgb').mkdir();(tmp_path/'depth').mkdir()
    rows,commands=[],[]
    command={'linear_mps':.4,'angular_rps':0.}
    for i in range(n):
        image=tmp_path/'rgb'/f'{i:05d}.jpg';Image.new('RGB',(4,2),(i,0,0)).save(image)
        depth=tmp_path/'depth'/f'{i:05d}.npz';np.savez_compressed(depth,depth_m=np.ones((2,4),dtype=np.float32))
        sensor={'position':[0,.88,-i*.1],'rotation_xyzw':[0,0,0,1]}
        rows.append({'frame':i,'control_time_s':i*.25,'collided':False,'world_position':[0,0,-i*.1],
            'world_rotation_xyzw':[0,0,0,1], 'base_pose_se2':[i*.1,0,0],
            'incoming_command':command if i else None,'sensors':{'rgb':sensor,'depth':sensor},
            'rgb_sha256':digest(image),'depth_sha256':digest(depth)})
        if i:
            commands.extend([{'phase':'started','step':i-1,'command':command},
                             {'phase':'returned','step':i-1,'collided':False}])
    for name,values in [('world_states',rows),('commands',commands)]:
        (tmp_path/(name+'.jsonl')).write_text(''.join(json.dumps(v)+'\n' for v in values))
    write(tmp_path/'terminal.json',{'frame':n-1,'receipt_stop':True,'termination':'expert_stop','visible_goal_label':None})
    return tmp_path
