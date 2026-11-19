import math

import pytest

from nav_data.planner_chunks.full_prepare import (
    expand_shared_viewpoints,
    join_sources,
    sparse_reference,
    tasks_for_episode,
)
from nav_data.planner_chunks.prepare import hydrate_manifest
from nav_data.records import digest, write


def fixture():
    fg=[{'path_id':1,'scan':'room','path':['a','b','c'],'instructions':['Go ahead.','Walk forward.'],
         'new_instructions':[[['go'],['stop']],[['walk'],['wait']]],'chunk_view':[[[1,3],[3,3]],[[1,3],[3,3]]]}]
    ep={'episode_id':7,'trajectory_id':1,'scene_id':'mp3d/room/room.glb','instruction':{'instruction_text':'Go ahead. '},
        'start_rotation':[0,0,0,1],'reference_path':[[0,0,0],[0,0,-1],[0,0,-2]]}
    return fg,ep


def test_train_join_reports_unmapped_and_duplicate_routes():
    fg,ep=fixture()
    joined,excluded,missing=join_sources(fg,[ep,dict(ep,episode_id=8)])
    assert len(joined)==1 and excluded[0]['reason']=='duplicate_instruction_route'
    assert missing==[{'path_id':1,'instruction_index':1,'reason':'no_unique_continuous_train_reference'}]
    joined,excluded,_=join_sources(fg,[dict(ep,reference_path=[[0,0,0]])])
    assert not joined and excluded[0]['reason']=='scene_or_anchor_count_mismatch'


def test_geometry_boundaries_and_nonmoving_chunk_merge():
    fg,ep=fixture();joined,_,_=join_sources(fg,[ep]);ep=joined[0]
    legs=sparse_reference(ep,lambda a,b:[a,b])
    assert len(legs)==2 and all(math.isclose(leg['distance_m'],1.) for leg in legs)
    cfg={'short_distance_m':1.,'draws_per_mode':2,'seed':3,'start_radius_m':.5,'yaw_degrees':45}
    tasks,candidates,audit=tasks_for_episode(ep,legs,'room',cfg)
    assert len(tasks)==1 and len(candidates)==4
    assert tasks[0]['instruction']=='go stop' and tasks[0]['chunk_ids']==[0,1]
    assert tasks[0]['goal_position']==[0,0,-2]
    assert audit[0]['source_kind']=='ordered_sparse_train_reference'
    assert candidates[2]['reference_fraction']==.5
    assert 'rows' not in tasks[0] and tasks[0]['reference_guides']==ep['reference_path']
    assert 'source_frame' not in candidates[2]
    assert math.isclose(audit[0]['distance_m'],2.)


def test_expansion_uses_identical_train_viewpoints_and_keeps_lineage():
    fg,ep=fixture();joined,_,_=join_sources(fg,[ep]);fg[0]['heading']=1.2
    extra,missing=expand_shared_viewpoints(fg,joined)
    assert len(extra)==1 and not missing
    assert extra[0]['reference_path']==ep['reference_path']
    assert extra[0]['fg']['instruction_index']==1
    assert extra[0]['fg']['reference_origin']=='identical_train_scan_viewpoint_coordinates'
    other=dict(fg[0],path_id=2,path=['a','missing','c'])
    extra,missing=expand_shared_viewpoints(fg+[other],joined)
    assert len(extra)==1 and len(missing)==2


def test_missing_fg_chunk_annotation_is_reported_not_indexed():
    fg,ep=fixture();fg[0]['heading']=0;fg[0]['new_instructions']=fg[0]['new_instructions'][:1]
    joined,_,_=join_sources(fg,[ep])
    extra,missing=expand_shared_viewpoints(fg,joined)
    assert not extra and missing[0]['reason']=='missing_or_mismatched_fg_chunk_annotation'
    _,excluded,_=join_sources(fg,[dict(ep,instruction={'instruction_text':'Walk forward.'})])
    assert excluded[0]['reason']=='missing_or_mismatched_fg_chunk_annotation'


def test_sharded_manifest_loads_only_frozen_shards(tmp_path):
    shard=tmp_path/'scene.json'
    write(shard,{'tasks':[{'id':'t'}],'candidates':[{'id':'c'}],'grouping':[]})
    m={'task_shards':[str(shard)],'task_count':1,'candidate_count':1,'source_sha256':{str(shard):digest(shard)}}
    assert hydrate_manifest(m)['candidates']==[{'id':'c'}]
    with pytest.raises(ValueError,match='unfrozen'):
        hydrate_manifest(dict(m,source_sha256={}))
    with pytest.raises(ValueError,match='counts'):
        hydrate_manifest(dict(m,candidate_count=2))
