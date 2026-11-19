"""Observation-only local task proposals and explicit RGB-D pixel grounding."""
import base64
import math
from pathlib import Path

from nav_data.records import digest

REGION_TOOL = {'type':'function','function':{
    'name':'propose_local_task_region','description':'Propose a local instruction and a visible floor stopping region.',
    'parameters':{'type':'object','additionalProperties':False,'properties':{
        'instruction':{'type':'string','minLength':1},
        'target_view':{'type':'string','enum':['front','right','back','left']},
        'bbox':{'type':'array','minItems':4,'maxItems':4,'items':{'type':'integer','minimum':0,'maximum':1000}},
        'needs_review':{'type':'boolean'},'reason':{'type':'string'}},
        'required':['instruction','target_view','bbox','needs_review','reason']}}}

# Current collection objective is navigation to visible destinations at any distance.
REGION_PROMPT = '''Invent one navigation task to a clearly visible FLOOR stopping region in these four current views. There is no assigned route or destination. You may choose any unambiguous current-floor region visible in the images, nearby or farther away: there is no preferred distance or metric distance limit. First identify the destination and draw ONE small bounding box enclosing its visible FLOOR stopping PATCH, then write the instruction that reaches it. The robot may stop at any safe floor point inside that patch. Select floor in front of or beside a landmark, not the landmark object itself. Keep the entire patch on one semantic side of a doorway or landmark. Describe only route elements actually supported by the images; do not invent hidden rooms, unseen intermediate steps, or hidden endpoints. Directions must be relative to the robot's CURRENT FRONT heading: right view is 90 degrees right, back is a turn around, left is 90 degrees left. Do not refer to camera views or graphical annotations in the navigation instruction. Do not choose stairs or a pure rotation task in this translation-task collection. Use normalized bbox=[xmin,ymin,xmax,ymax] in [0,1000], origin top-left, x right, y down. Keep the box tight around a single stopping patch, not the whole room: each side at most 500 and box area at most 15 percent of the image. A geometry checker filters visible floor candidates inside the original box and cannot expand it. Set needs_review=true if a clear task cannot be identified. Return propose_local_task_region. No movement is performed by this tool.'''


def build_request(public, profile, folder, *, target_mode='box'):
    if set(public) != {'images'} or set(public['images']) != {'front', 'right', 'back', 'left'}:
        raise ValueError('only four current images may be sent to the task proposer')
    if target_mode != 'box':
        raise ValueError('short-instruction mainline requires box targets')
    tool=REGION_TOOL
    prompt=REGION_PROMPT
    text='Invent one task within the current visible scene and identify its visible floor destination. Near and farther visible destinations are equally allowed.'
    text+=' Front is the current forward heading; right is 90 degrees clockwise, back is 180 degrees, left is 90 degrees counterclockwise. Write directions relative to the current FRONT heading, not relative to the selected camera.'
    content = [{'type': 'text', 'text': text}]
    for label in ('front', 'right', 'back', 'left'):
        item = public['images'][label]
        if set(item) != {'path', 'sha256'}:
            raise ValueError('unexpected public image metadata')
        p = Path(item['path']).resolve()
        if p.parent != Path(folder).resolve() or p.name != label+'.png' or digest(p) != item['sha256']:
            raise ValueError('current image identity mismatch')
        content += [{'type': 'text', 'text': 'Current view: '+label},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,'+
                     base64.b64encode(p.read_bytes()).decode()}}]
    return {'model': profile['model'], 'messages': [{'role': 'system', 'content': prompt},
            {'role': 'user', 'content': content}], 'tools': [tool],
            'tool_choice': {'type': 'function', 'function': {'name': tool['function']['name']}},
            'parallel_tool_calls': False, 'temperature': 0, 'max_tokens': 1024,
            'chat_template_kwargs': {'enable_thinking': False}}


def backproject(u, v, depth, intrinsics, sensor):
    """Normalized RGB pixel + axial -Z depth -> Habitat world XYZ, with xyzw quaternion."""
    import numpy as np
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and 0 <= x <= 1000 for x in (u,v)):
        raise ValueError('normalized point outside image')
    if depth.ndim != 2:
        raise ValueError('depth must be an image')
    height, width = depth.shape
    px, py = int(round(u*(width-1)/1000)), int(round(v*(height-1)/1000))
    distance = float(depth[py,px])
    if not math.isfinite(distance) or distance <= 0:
        raise ValueError('selected point has invalid depth')
    k = np.asarray(intrinsics,dtype=float)
    q = np.asarray(sensor['rotation_xyzw'],dtype=float)
    position = np.asarray(sensor['position'],dtype=float)
    if (k.shape != (3,3) or q.shape != (4,) or position.shape != (3,)
            or not np.isfinite(k).all() or not np.isfinite(q).all() or not np.isfinite(position).all()
            or k[0,0] <= 0 or k[1,1] <= 0 or not np.isclose(np.linalg.norm(q),1,atol=1e-5)):
        raise ValueError('invalid sensor calibration')
    local = np.asarray([(px-k[0,2])*distance/k[0,0], -(py-k[1,2])*distance/k[1,1], -distance])
    rotated = local + 2*np.cross(q[:3],np.cross(q[:3],local)+q[3]*local)
    return {'pixel_xy': [px,py], 'depth_m': distance, 'camera_point': local.tolist(),
            'world_point': (position+rotated).tolist()}


def validate_floor_target(point, snapped, start, *, horizontal_limit=.05, vertical_limit=.20):
    """Allow NavMesh voxel-height offset without moving a selected point across a doorway/wall."""
    import numpy as np
    point,snapped,start=(np.asarray(p,dtype=float) for p in (point,snapped,start))
    if any(p.shape!=(3,) or not np.isfinite(p).all() for p in (point,snapped,start)):
        raise ValueError('invalid grounded target')
    horizontal=float(np.linalg.norm((point-snapped)[[0,2]]))
    vertical=float(abs(point[1]-snapped[1]))
    if horizontal>horizontal_limit or vertical>vertical_limit or abs(snapped[1]-start[1])>.20:
        raise ValueError('selected pixel is not near navigable current-floor surface')
    return {'horizontal_projection_m':horizontal,'vertical_projection_m':vertical,
            'horizontal_limit_m':horizontal_limit,'vertical_limit_m':vertical_limit}
