"""Immutable train-only replenishment: reuse good slots; never overwrite old attempts."""
import copy
import hashlib
import json
import math
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from nav_data.collection.pool import ModelPool
from nav_data.planner_chunks.full_prepare import CE_TRAIN_SHA, FG_TRAIN_SHA
from nav_data.planner_chunks.prepare import (
    code_identity,
    freeze_relaxed_defaults,
    hydrate_manifest,
    validate_config,
)
from nav_data.records import digest, read, write
from nav_data.tasks.instruction import annotate


def task_identity(task):
    fg = task['fg']
    ids = task['chunk_ids']
    return {'path_id': fg['path_id'], 'instruction_index': fg['instruction_index'],
            'chunk_ids': ids, 'chunk_intervals': [fg['chunk_view'][i] for i in ids],
            'instruction': ' '.join(task['instruction'].split()), 'scene': str(Path(task['scene']).resolve())}


def slot_compatible(current_task, old_task, current_candidate, old_candidate):
    if task_identity(current_task) != task_identity(old_task):
        return False
    if current_candidate['id'] != old_candidate['id'] or current_candidate['mode'] != old_candidate['mode']:
        return False
    if math.dist(current_candidate['offset'], old_candidate['offset']) > 1e-10:
        return False
    if abs(current_candidate['yaw_delta']-old_candidate['yaw_delta']) > 1e-10:
        return False
    a,b = current_task['goal_position'],old_task['goal_position']
    return math.hypot(a[0]-b[0],a[2]-b[2]) <= .03 and abs(a[1]-b[1]) <= .25


def geometry_usable(result, terminal):
    record=result.get('geometry') or {}
    geometry=record.get('geometry') or {}
    return (record.get('status')=='geometry_complete' and geometry.get('passed') is True
            and geometry.get('collision_steps')==0 and geometry.get('termination')=='expert_stop'
            and terminal.get('receipt_stop') is True and terminal.get('termination')=='expert_stop'
            and terminal.get('frame')==geometry.get('steps'))


def movement_settled(folder):
    """Only completed command pairs prove that a failed source has no open movement."""
    path=Path(folder)/'commands.jsonl'
    if not path.exists():
        record=Path(folder)/'geometry-result.json'
        if not record.exists():return False
        geometry=read(record)
        return geometry.get('stage') in ('preflight','observation') or geometry.get('status')=='excluded_before_movement'
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    if len(rows)%2:
        return False
    return all(a.get('phase')=='started' and b.get('phase')=='returned' and a.get('step')==b.get('step')
               for a,b in zip(rows[::2],rows[1::2]))


def verified_manifest(root, cache):
    root=Path(root).resolve()
    if digest(root/'manifest.json') != read(root/'manifest.sha256.json')['sha256']:
        raise ValueError('source manifest checksum mismatch')
    manifest=read(root/'manifest.json');validate_config(manifest['config'])
    for path,expected in {**manifest['source_sha256'],**manifest['code_sha256']}.items():
        key=(path,expected)
        if key not in cache:
            if digest(path)!=expected:
                raise ValueError(f'frozen source changed: {path}')
            cache.add(key)
    values=set(manifest['source_sha256'].values())
    if FG_TRAIN_SHA not in values or CE_TRAIN_SHA not in values:
        raise ValueError('replenishment requires the same pinned FG and continuous train sources')
    return hydrate_manifest(manifest)


def write_index(root, name, rows):
    files={};paths=[]
    try:
        for row in rows:
            scene=Path(row['scene']).stem
            if scene not in files:
                path=Path(root)/name/(scene+'.jsonl');path.parent.mkdir(parents=True,exist_ok=True)
                files[scene]=path.open('x');paths.append(path)
            files[scene].write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
    finally:
        for stream in files.values():stream.close()
    return paths


def prepare_replenishment(base_root, prior_roots, output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    cache=set();base=verified_manifest(base_root,cache)
    if any('reference_guides' not in t for t in base['tasks']):
        raise ValueError('base must use sparse-reference tasks')
    tasks={t['id']:t for t in base['tasks']};candidates={c['id']:c for c in base['candidates']}
    if len(candidates)!=len(base['candidates']):raise ValueError('duplicate base slots')
    config=copy.deepcopy(base['config']);freeze_relaxed_defaults(config);validate_config(config)
    owner=uuid.uuid4().hex
    coverage={};geometry_only={};prior_failures={};unsettled={};stats=Counter();source_hashes=dict(base['source_sha256'])
    priors=[]
    expanded=[]
    for root in prior_roots:
        root=Path(root).resolve()
        child=root/'annotation-only'
        if (child/'manifest.json').exists() and (child/'summary.json').exists():
            expanded.append(child)
        expanded.append(root)
    prior_roots=list(dict.fromkeys(expanded))
    # Callers list newest completed attempts first, so already valid revised samples win.
    for prior_root in prior_roots:
        prior_root=Path(prior_root).resolve();prior=verified_manifest(prior_root,cache)
        attempt=prior_root if (prior_root/'summary.json').exists() and (prior_root/'candidates').exists() else prior_root/'attempt'
        summary=read(attempt/'summary.json')
        if summary['owner']!=prior['owner'] or summary.get('model_threads_still_running'):
            raise ValueError('source attempt is not sealed')
        prior_tasks={t['id']:t for t in prior['tasks']}
        priors.append({'manifest_dir':str(prior_root),'owner':prior['owner'],'counts':summary['counts']})
        for path in (prior_root/'manifest.json',prior_root/'manifest.sha256.json',attempt/'summary.json'):
            source_hashes[str(path)]=digest(path)
        for candidate in prior['candidates']:
            cid=candidate['id']
            if cid not in candidates or cid in coverage:
                continue
            task=tasks[candidates[cid]['task_id']]
            if not slot_compatible(task,prior_tasks[candidate['task_id']],candidates[cid],candidate):
                stats['incompatible_prior_slots']+=1;continue
            folder=attempt/'candidates'/cid
            data=(folder/'result.json').read_bytes();result=json.loads(data)
            source={'candidate_dir':str(folder),'owner':prior['owner'],
                    'result_sha256':hashlib.sha256(data).hexdigest(),'status':result['status'],
                    'reference_policy':prior.get('reference_policy'),
                    'initial_state_equivalence':'logical_augmentation_slot_only; preserve_recorded_pose'}
            if result['status']=='excluded_before_movement':
                prior_failures.setdefault(cid,source);continue
            terminal=read(folder/'terminal.json') if (folder/'terminal.json').exists() else {}
            if geometry_usable(result,terminal):
                lease=read(folder/'lease.json')
                if lease['owner']!=prior['owner']:
                    raise ValueError('source lease ownership mismatch')
                source.update(geometry_sha256=digest(folder/'geometry-result.json'),
                              terminal_sha256=digest(folder/'terminal.json'),
                              camera_schema_version=read(folder/'camera.json').get('schema_version',1))
                if (result.get('model') or {}).get('status')=='completed' and result['status']=='completed_audit':
                    coverage[cid]=source
                else:
                    geometry_only.setdefault(cid,source)
            else:
                if source['status']=='failed_or_uncertain':
                    source['movement_settled']=movement_settled(folder)
                    if not source['movement_settled']:unsettled[cid]=source
                prior_failures.setdefault(cid,source)
    actions=[];pending=[];annotation_jobs=[];held=[]
    for candidate in base['candidates']:
        cid=candidate['id'];scene=candidate['scene']
        row={'candidate_id':cid,'task_id':candidate['task_id'],'scene':scene,'split':'train','admission':'audit_only'}
        if cid in coverage:
            row.update(status='reused_completed',source=coverage[cid])
        elif cid in geometry_only:
            row.update(status='annotation_only_pending',source=geometry_only[cid])
            annotation_jobs.append(dict(candidate,source=geometry_only[cid],task=tasks[candidate['task_id']]))
        elif cid in unsettled:
            row.update(status='held_uncertain_movement',source=unsettled[cid]);held.append(cid)
        else:
            row.update(status='simulation_pending',prior_source=prior_failures.get(cid))
            pending.append(candidate)
        actions.append(row)
    counts=Counter(row['status'] for row in actions)
    index_paths=write_index(output,'coverage-index',actions)
    write(output/'inventory.json',{'owner':owner,'base_slots':len(candidates),'counts':dict(counts),
          'prior_attempts':priors,'held_uncertain_candidates':held,'diagnostics':dict(stats),
          'reuse_policy':'valid prior sample in identical task/augmentation slot; actual old state and version retained',
          'automatic_retry':False,'explicit_run_request':'user requested full replenishment and skipping completed samples'})
    write(output/'annotation-jobs.json',annotation_jobs)
    pending_tasks={c['task_id'] for c in pending};by_scene=defaultdict(list)
    for c in pending:by_scene[c['scene']].append(c)
    shard_paths=[]
    for index,(scene,jobs) in enumerate(sorted(by_scene.items())):
        path=output/'task-shards'/f'{index:03d}.json'
        write(path,{'tasks':[t for t in base['tasks'] if t['id'] in pending_tasks and t['scene']==scene],
                    'candidates':jobs,'grouping':[]})
        shard_paths.append(path)
    for path in index_paths+shard_paths+[output/'inventory.json',output/'annotation-jobs.json']:
        source_hashes[str(path)]=digest(path)
    base_root=Path(base_root).resolve()
    for name in ('manifest.json','manifest.sha256.json'):
        source_hashes[str(base_root/name)]=digest(base_root/name)
    manifest={k:copy.deepcopy(v) for k,v in base.items() if k not in ('tasks','candidates','grouping','task_shards')}
    manifest.update(owner=owner,config=config,task_shards=[str(p) for p in shard_paths],
        task_count=len(pending_tasks),candidate_count=len(pending),source_sha256=source_hashes,
        code_sha256=code_identity(),reference_policy='sparse-plan-comfort-fallback-goal-region-v3',
        replenishment={'base_owner':base['owner'],'coverage_shards':[str(p) for p in index_paths],
                       'annotation_jobs':str(output/'annotation-jobs.json'),'counts':dict(counts)})
    write(output/'manifest.json',manifest);write(output/'manifest.sha256.json',{'sha256':digest(output/'manifest.json')})
    return manifest


def run_annotation_only(manifest, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    jobs=read(manifest['replenishment']['annotation_jobs'])
    model_manifest={k:copy.deepcopy(v) for k,v in manifest.items() if k not in ('task_shards','tasks','candidates','grouping')}
    model_manifest.update(tasks=list({j['task']['id']:j['task'] for j in jobs}.values()),
        candidates=[{k:v for k,v in j.items() if k not in ('task','source')} for j in jobs],
        task_count=len({j['task_id'] for j in jobs}),candidate_count=len(jobs),
        execution_mode='annotation_only_with_reused_prior_geometry',
        geometry_sources={j['id']:j['source'] for j in jobs})
    write(output/'manifest.json',model_manifest)
    write(output/'manifest.sha256.json',{'sha256':digest(output/'manifest.json')})
    profiles=manifest['config']['model_profiles']
    results=[]
    def run(job,profile):
        cid=job['id'];source=Path(job['source']['candidate_dir']);folder=output/'candidates'/cid
        folder.mkdir(parents=True,exist_ok=False)
        if digest(source/'result.json')!=job['source']['result_sha256']:
            raise ValueError('reused source changed')
        write(folder/'lease.json',{'owner':manifest['owner'],'candidate_id':cid,'scope':'annotation_only',
              'movement_performed':False,'source':job['source'],'automatic_retry':False})
        public=read(source/'observation.json')
        for label,item in public['images'].items():
            original=Path(item['path'])
            if original.resolve().parent!=source.resolve() or digest(original)!=item['sha256']:
                raise ValueError('old current-observation image integrity mismatch')
            with (folder/(label+'.png')).open('xb') as stream:stream.write(original.read_bytes())
            item['path']=str((folder/(label+'.png')).resolve())
        write(folder/'observation.json',public)
        for name in ('camera.json','terminal.json','world_states.jsonl','commands.jsonl','private-geometry.json','geometry-result.json'):
            with (folder/name).open('xb') as stream:stream.write((source/name).read_bytes())
        for name in ('rgb','depth'):(folder/name).symlink_to(source/name,target_is_directory=True)
        started=time.perf_counter()
        write(folder/'model-started.json',{'owner':manifest['owner'],'profile':profile['id'],
              'started_unix':time.time(),'automatic_retry':False,'geometry_reused':True})
        try:
            value=annotate(public,profile,folder,manifest['config']['request_timeout_s'])
            model={'status':'completed','annotation':value}
        except Exception as error:
            model={'status':'failed_or_uncertain','error':str(error),'automatic_retry':False}
        model.update(wall_s=time.perf_counter()-started,profile=profile['id'])
        write(folder/'model-result.json',model)
        result={'candidate_id':cid,'status':'completed_audit' if model['status']=='completed' else 'failed_or_uncertain',
                'geometry':read(source/'geometry-result.json'),'model':model,'geometry_source':job['source'],
                'movement_performed':False,'automatic_retry':False,'admission':'audit_only'}
        write(folder/'result.json',result)
        return result
    def safe_run(job,profile):
        try:
            return run(job,profile)
        except Exception as error:
            folder=output/'candidates'/job['id'];folder.mkdir(parents=True,exist_ok=True)
            result={'candidate_id':job['id'],'status':'failed_or_uncertain',
                    'geometry_source':job['source'],'error':repr(error),'stage':'annotation_source_preparation',
                    'movement_performed':False,'automatic_retry':False,'admission':'audit_only'}
            if not (folder/'result.json').exists():write(folder/'result.json',result)
            return result
    def consume(job,profile,enqueued):
        results.append(safe_run(job,profile))
    pool=ModelPool(profiles,manifest['config']['ready_queue_size'],consume).start()
    try:
        for job in jobs:pool.submit(job)
        pool.close(progress_timeout=manifest['config']['request_timeout_s']+5)
    except BaseException:
        pool.close(cancel=True)
        raise
    pool.check()
    summary={'owner':manifest['owner'],'counts':dict(Counter(r['status'] for r in results)),
             'movement_steps':0,'automatic_retry':False,'concurrency':pool.stats()}
    write(output/'summary.json',summary)
    return summary


def finalize_coverage(manifest, root):
    root=Path(root);rows=[]
    for path in manifest['replenishment']['coverage_shards']:
        for line in Path(path).read_text().splitlines():
            row=json.loads(line);cid=row['candidate_id'];state=row['status']
            if state=='reused_completed':
                row.update(status='completed_audit',candidate_dir=row['source']['candidate_dir'],origin='prior_completed')
            elif state in ('simulation_pending','annotation_only_pending'):
                folder=root/('attempt' if state=='simulation_pending' else 'annotation-only')/'candidates'/cid
                result=read(folder/'result.json')
                row.update(status=result['status'],candidate_dir=str(folder),origin=state,
                           result_sha256=digest(folder/'result.json'))
            rows.append(row)
    paths=write_index(root,'final-coverage-index',rows)
    summary={'owner':manifest['owner'],'total_slots':len(rows),
             'counts':dict(Counter(row['status'] for row in rows)),
             'index_sha256':{str(p):digest(p) for p in paths},'admission':'audit_only','training_exports':0}
    write(root/'coverage-summary.json',summary)
    return summary
