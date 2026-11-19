import hashlib
import math

import numpy as np
import pytest
from PIL import Image

from nav_data.tasks.region import (
    REGION_PROMPT,
    backproject,
    build_request,
    validate_floor_target,
)


def test_axial_depth_backprojection_reaches_floor_and_rotates_to_world():
    k=[[100,0,100],[0,100,50],[0,0,1]]
    sensor={'position':[1,1,2],'rotation_xyzw':[0,0,0,1]}
    point=backproject(500,1000,np.full((101,201),2.),k,sensor)
    assert point['world_point']==pytest.approx([1,0,0])
    sensor['rotation_xyzw']=[0,math.sin(math.pi/4),0,math.cos(math.pi/4)]
    assert backproject(500,1000,np.full((101,201),2.),k,sensor)['world_point']==pytest.approx([-1,0,2])


@pytest.mark.parametrize('u,v,d',[(1001,500,2),(500,-1,2),(float('nan'),500,2),(500,500,0),(500,500,float('inf'))])
def test_invalid_pixel_or_depth_rejected(u,v,d):
    with pytest.raises(ValueError):
        backproject(u,v,np.full((3,3),d),np.eye(3),{'position':[0,0,0],'rotation_xyzw':[0,0,0,1]})


def test_public_request_rejects_assigned_tasks_and_private_targets(tmp_path):
    profile={'model':'test'}
    for extra in ['instruction','goal_position','reference_path','evaluation_metrics']:
        with pytest.raises(ValueError,match='only four'):
            build_request({'images':{},extra:'private'},profile,tmp_path)


def test_floor_voxel_height_offset_allowed_without_horizontal_semantic_shift():
    result=validate_floor_target([0,-.003,0],[0,.101,0],[1,.101,1])
    assert result['horizontal_projection_m']==0
    assert result['vertical_projection_m']==pytest.approx(.104)
    for point,snapped in [([0,0,0],[.06,.1,0]),([0,.9,0],[0,.1,0]),([0,3,0],[0,3,0])]:
        with pytest.raises(ValueError):validate_floor_target(point,snapped,[1,.1,1])


def test_mainline_uses_all_views_and_robot_relative_directions_without_forcing_a_view(tmp_path):
    images={}
    for label in ['front','right','back','left']:
        p=tmp_path/(label+'.png');Image.new('RGB',(4,3)).save(p)
        images[label]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    request=build_request({'images':images},{'model':'test'},tmp_path)
    content=request['messages'][1]['content']
    assert len([x for x in content if x['type']=='image_url'])==4
    assert 'For this camera-coverage sample' not in content[0]['text']
    assert 'current FRONT heading' in content[0]['text']
    assert 'reference_path' not in str(request) and 'Assigned local task' not in str(request)
    with pytest.raises(ValueError,match='requires box'):
        build_request({'images':images},{'model':'test'},tmp_path,target_mode='point')


def test_visible_prompts_remove_short_range_bias_but_keep_observation_grounding():
    assert '1-4' not in REGION_PROMPT and 'no preferred distance' in REGION_PROMPT
    assert 'hidden' in REGION_PROMPT and 'CURRENT FRONT' in REGION_PROMPT
