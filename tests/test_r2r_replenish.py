import copy
from pathlib import Path

from nav_data.planner_chunks import replenish
from nav_data.planner_chunks.prepare import hydrate_manifest
from nav_data.records import digest, read, write


def config():
    return {'split':'train','max_tasks':8,'max_candidates':32,'draws_per_mode':2,'ready_queue_size':2,
        'candidate_timeout_s':30,'request_timeout_s':2,'max_steps':400,'start_radius_m':.5,
        'yaw_degrees':45,'short_distance_m':1.,'seed':1,'simulation_workers':[{'id':'w','gpu':0}],
        'model_profiles':[{'id':'m','endpoint':'http://unused/v1/chat/completions','model':'fake','slots':1}],
        'source_collection':'unused','source_review':'unused','snapshot_root':'unused','base_request':'unused'}


def fixture(tmp_path):
    task={'id':'task','fg':{'path_id':1,'instruction_index':0,'chunk_view':[[1,2]]},
          'chunk_ids':[0],'instruction':'Go through the door.','scene':'/scenes/room.glb',
          'goal_position':[1,0,0],'reference_guides':[[0,0,0],[1,0,0]]}
    candidates=[{'id':f'task-{i}','task_id':'task','scene':task['scene'],'mode':'start' if i<2 else 'mid',
                 'offset':[.1,0,0],'yaw_delta':.1,'reference_fraction':0. if i<2 else .5} for i in range(4)]
    base={'owner':'base','config':config(),'tasks':[task],'candidates':candidates,'runtime':{},
          'source_sha256':{},'code_sha256':{},'snapshot_root':'unused','admission':'audit_only'}
    prior=copy.deepcopy(base);prior['owner']='prior'
    for name,m in [('base',base),('prior',prior)]:
        root=tmp_path/name
        write(root/'manifest.json',m);write(root/'manifest.sha256.json',{'sha256':digest(root/'manifest.json')})
    write(tmp_path/'prior/attempt/summary.json',{'owner':'prior','model_threads_still_running':0,'counts':{}})
    for i,candidate in enumerate(candidates):
        folder=tmp_path/'prior/attempt/candidates'/candidate['id']
        geometry={'status':'geometry_complete','geometry':{'passed':True,'termination':'expert_stop','collision_steps':0,'steps':0}}
        status='completed_audit' if i==0 else 'failed_or_uncertain' if i in (1,3) else 'excluded_before_movement'
        if i==2:geometry={'status':'excluded_before_movement','stage':'preflight','error':'no path'}
        if i==3:geometry={'status':'failed_or_uncertain','stage':'movement','error':'lost response'}
        write(folder/'geometry-result.json',geometry)
        write(folder/'result.json',{'candidate_id':candidate['id'],'status':status,'geometry':geometry,
                                   'model':{'status':'completed' if i==0 else 'failed_or_uncertain'}})
        if i<2:
            write(folder/'terminal.json',{'frame':0,'receipt_stop':True,'termination':'expert_stop'})
            write(folder/'lease.json',{'owner':'prior'})
            write(folder/'camera.json',{'schema_version':2})
        if i==3:(folder/'commands.jsonl').write_text('{"phase":"started","step":0}\n')
    return base,prior


def test_replenish_reuses_success_and_geometry_but_holds_uncertain_movement(tmp_path,monkeypatch):
    base,prior=fixture(tmp_path)
    monkeypatch.setattr(replenish,'verified_manifest',lambda root,cache: {'base':base,'prior':prior}[Path(root).name])
    original=digest(tmp_path/'prior/attempt/candidates/task-0/result.json')
    m=replenish.prepare_replenishment(tmp_path/'base',[tmp_path/'prior'],tmp_path/'new')
    assert m['replenishment']['counts']=={'reused_completed':1,'annotation_only_pending':1,
                                        'simulation_pending':1,'held_uncertain_movement':1}
    loaded=hydrate_manifest(m)
    assert [c['id'] for c in loaded['candidates']]==['task-2']
    assert [j['id'] for j in read(tmp_path/'new/annotation-jobs.json')]==['task-1']
    assert digest(tmp_path/'prior/attempt/candidates/task-0/result.json')==original
    assert not (tmp_path/'new/attempt').exists()


def test_same_id_different_instruction_boundary_or_draw_is_not_equivalent(tmp_path):
    base,_=fixture(tmp_path)
    t,c=base['tasks'][0],base['candidates'][0]
    assert replenish.slot_compatible(t,t,c,c)
    changed=copy.deepcopy(t);changed['instruction']='Stay outside the door.'
    assert not replenish.slot_compatible(t,changed,c,c)
    changed=copy.deepcopy(t);changed['fg']['chunk_view'][0]=[1,3]
    assert not replenish.slot_compatible(t,changed,c,c)
    changed=copy.deepcopy(c);changed['offset'][0]=.2
    assert not replenish.slot_compatible(t,t,c,changed)


def test_only_paired_completed_movement_journal_is_settled(tmp_path):
    path=tmp_path/'commands.jsonl'
    path.write_text('{"phase":"started","step":0}\n')
    assert not replenish.movement_settled(tmp_path)
    path.write_text('{"phase":"started","step":0}\n{"phase":"returned","step":0}\n')
    assert replenish.movement_settled(tmp_path)
    path.write_text('{"phase":"started","step":0}\n{"phase":"returned","step":1}\n')
    assert not replenish.movement_settled(tmp_path)


def test_annotation_only_reuses_recording_without_simulation(tmp_path,monkeypatch):
    base,prior=fixture(tmp_path)
    monkeypatch.setattr(replenish,'verified_manifest',lambda root,cache: {'base':base,'prior':prior}[Path(root).name])
    m=replenish.prepare_replenishment(tmp_path/'base',[tmp_path/'prior'],tmp_path/'new')
    source=tmp_path/'prior/attempt/candidates/task-1'
    images={}
    for view in ('front','right','back','left'):
        path=source/(view+'.png');path.write_bytes(b'fake-current-image')
        images[view]={'path':str(path),'sha256':digest(path)}
    write(source/'observation.json',{'instruction':'Go through the door.','images':images})
    write(source/'private-geometry.json',{'source':'actual_prior_execution'})
    (source/'world_states.jsonl').write_text('{}\n');(source/'commands.jsonl').write_text('')
    (source/'rgb').mkdir();(source/'depth').mkdir()
    before=digest(source/'world_states.jsonl')
    called=[]
    def fake(public,profile,folder,timeout):
        called.append(folder)
        assert all(Path(item['path']).parent==folder for item in public['images'].values())
        return {'instruction':'Enter the door.','needs_review':False,'reason':''}
    monkeypatch.setattr(replenish,'annotate',fake)
    summary=replenish.run_annotation_only(m,tmp_path/'new/annotation-only')
    assert summary['counts']=={'completed_audit':1} and summary['movement_steps']==0
    assert len(called)==1
    repaired=tmp_path/'new/annotation-only/candidates/task-1'
    assert (repaired/'rgb').is_symlink()
    assert digest(source/'world_states.jsonl')==before==digest(repaired/'world_states.jsonl')
    assert read(repaired/'lease.json')['scope']=='annotation_only'
    assert read(repaired/'result.json')['movement_performed'] is False
    assert read(tmp_path/'new/annotation-only/manifest.json')['execution_mode']=='annotation_only_with_reused_prior_geometry'
