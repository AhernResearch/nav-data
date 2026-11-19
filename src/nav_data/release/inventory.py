#!/usr/bin/env python3
"""Freeze and audit compact free-collection episodes without choosing training samples."""
import csv
import datetime
import gzip
import hashlib
import io
import json
import math
import shutil
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from nav_data.collection.distribution import snapshot_entries, summarize_episode
from nav_data.release.compact import SCHEMA, validate_data
from nav_data.release.evidence import request_digest


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for b in iter(lambda:stream.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def save(path,value):
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)


def load_data(entry):
    path=Path(entry['metadata'])
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=entry['metadata_sha256']:raise ValueError('metadata/catalog hash mismatch')
    data=json.loads(raw)
    if data['schema']!=SCHEMA or data['request_id']!=entry['request_id'] or data['id']!=entry['case_id']:
        raise ValueError('metadata identity mismatch')
    if data['client_folder']!=str(Path(entry['client_root'])/entry['case_id']) or path!=Path(data['raw_folder'])/'meta.json':
        raise ValueError('metadata path scope mismatch')
    for key,value in entry['frozen_original_sha256'].items():
        if data['original_sha256'].get(key)!=value:raise ValueError('original source hash mismatch')
    return data


def check_episode(entry):
    try:
        data=load_data(entry)
        validate_data(data,media=True,full=False)
        states=data['world_states'];raw=data['records']['raw'];client=data['records']['client']
        if len(states)!=entry['frames']:raise ValueError('catalog frame count mismatch')
        metrics=summarize_episode(data,{'format':'compact','sha256':entry['metadata_sha256']})
        instruction=metrics['instruction']
        if not isinstance(instruction,str) or not instruction.strip():raise ValueError('empty instruction')
        position=np.asarray([s['world_position'] for s in states])
        se2=np.asarray([s['base_pose_se2'] for s in states])
        q=np.asarray([s['world_rotation_xyzw'] for s in states])
        yaw=np.arctan2(2*(q[:,0]*q[:,2]+q[:,3]*q[:,1]),1-2*(q[:,0]**2+q[:,1]**2))
        yaw_error=np.arctan2(np.sin(se2[:,2]-yaw),np.cos(se2[:,2]-yaw))
        if not np.isfinite(se2).all() or not np.allclose(se2[:,:2],-position[:,[2,0]],atol=1e-6,rtol=0) or np.max(np.abs(yaw_error))>1e-5:
            raise ValueError('body SE2/world-pose mismatch')
        if any(not all(math.isfinite(v) for v in s['incoming_command'].values()) for s in states[1:]):
            raise ValueError('nonfinite control command')
        proposal=client['proposed-task.json'];camera=raw['camera.json']
        public=client['public-observation.json']['images']
        normalized=' '.join(instruction.casefold().split())
        content=[(s['world_position'],s['world_rotation_xyzw'],s['rgb_sha256'],s['depth_sha256']) for s in states]
        recording_hash=hashlib.sha256(canonical([metrics['scene'],content])).hexdigest()
        duplicate_hash=hashlib.sha256(canonical([recording_hash,normalized,metrics['goal']])).hexdigest()
        tags=[]
        if proposal.get('needs_review'):tags.append('proposal_needs_review')
        if raw['terminal.json'].get('endpoint_review_required'):tags.append('endpoint_review_required')
        if raw['terminal.json'].get('goal_adjustment_m',0)>0:tags.append('goal_adjusted')
        if metrics['path_m']>=15:tags.append('path_at_least_15m')
        if metrics['final_error_m']>.08:tags.append('final_error_above_8cm_within_success_tolerance')
        row=dict(uid=data['request_id'],case_id=data['id'],run=Path(entry['client_root']).parent.name,
            scene=Path(metrics['scene']).stem,source_split=client['pointnav-target.json']['source_split'],
            split_assignment='unassigned',training_admission=data['training_admission'],
            semantic_admission=client['result.json'].get('semantic_admission','pending_review'),
            metadata=entry['metadata'],metadata_sha256=entry['metadata_sha256'],client_root=entry['client_root'],
            frames=len(states),duration_s=metrics['duration_s'],instruction=instruction,
            normalized_instruction_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
            recording_fingerprint=recording_hash,exact_episode_fingerprint=duplicate_hash,
            start=metrics['start'],goal=metrics['goal'],endpoint=metrics['endpoint'],
            path_m=metrics['path_m'],goal_planar_m=metrics['goal_planar_m'],final_error_m=metrics['final_error_m'],
            target_view=metrics['target_view'],collision_steps=metrics['collision_steps'],
            terminal_frame=raw['terminal.json']['frame'],native_expert_stop_verified=True,
            body_pose_reference='meta.json#world_states[*].base_pose_se2',
            command_reference='meta.json#commands; command step i leads from frame i to i+1',
            front_rgb_template=str(Path(data['raw_folder'])/'rgb/{frame:05d}.jpg'),
            front_depth_template=str(Path(data['raw_folder'])/'depth/{frame:05d}.npz'),
            initial_views={v:dict(path=public[v]['path'],sha256=public[v]['sha256'],frame=0) for v in ['front','right','back','left']},
            sequence_views=['front'],camera=camera['camera'],resolution_wh=camera['resolution_wh'],
            control_dt_s=camera['control_dt_s'],model=data['model'],review_tags=tags,
            technical_qc='passed_all_metadata_and_media_file_sets',
            deep_media_qc='not_selected',frozen_original_sha256=entry['frozen_original_sha256'])
        if row['source_split']!='train':raise ValueError('non-train source')
        return row
    except Exception as error:
        return dict(error=repr(error),entry=entry)


def audit_media(row):
    try:
        entry=dict(metadata=row['metadata'],metadata_sha256=row['metadata_sha256'],request_id=row['uid'],
            case_id=row['case_id'],client_root=row['client_root'],frozen_original_sha256=row['frozen_original_sha256'])
        data=load_data(entry);root=Path(data['raw_folder']);size=data['records']['raw']['camera.json']['resolution_wh']
        total_bytes=0
        for view,item in row['initial_views'].items():
            content=Path(item['path']).read_bytes();total_bytes+=len(content)
            if hashlib.sha256(content).hexdigest()!=item['sha256']:raise ValueError('initial view hash mismatch: '+view)
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if list(image.size)!=size:raise ValueError('initial view resolution mismatch')
        for i,state in enumerate(data['world_states']):
            for kind,suffix in [('rgb','.jpg'),('depth','.npz')]:
                content=(root/kind/(f'{i:05d}'+suffix)).read_bytes();total_bytes+=len(content)
                if hashlib.sha256(content).hexdigest()!=state[kind+'_sha256']:raise ValueError('frame media hash mismatch')
                if kind=='rgb':
                    with Image.open(io.BytesIO(content)) as image:
                        image.load()
                        if list(image.size)!=size:raise ValueError('frame RGB resolution mismatch')
                else:
                    with np.load(io.BytesIO(content),allow_pickle=False) as z:
                        depth=z['depth_m']
                        if depth.shape!=(size[1],size[0]) or depth.dtype!=np.float32 or not np.isfinite(depth).all() or not (depth>0).any():
                            raise ValueError('invalid metric depth frame')
        return dict(uid=row['uid'],passed=True,frames=row['frames'],initial_views=4,bytes_hashed_and_decoded=total_bytes)
    except Exception as error:
        return dict(uid=row['uid'],passed=False,error=repr(error))


def select_deep(rows):
    groups=defaultdict(list)
    for row in rows:groups[row['scene']].append(row)
    selected={}
    for scene,group in groups.items():
        ordered=sorted(group,key=lambda r:hashlib.sha256(('free-qc-v1:'+r['uid']).encode()).hexdigest())
        for row in ordered[:2]:selected[row['uid']]=row
        for view in ['front','right','back','left']:
            row=next((r for r in ordered if r['target_view']==view),None)
            if row is not None:selected[row['uid']]=row
        row=max(group,key=lambda r:(r['frames'],r['uid']));selected[row['uid']]=row
    return sorted(selected.values(),key=lambda r:(r['scene'],r['uid']))


def csv_rows(path,rows,fields):
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        writer.writerows({k:row[k] for k in fields} for row in rows)


def build_inventory(args):
    result=json.loads((args.conversion_root/'result.json').read_text())
    assert result['passed'] and result['status']=='completed' and result['total_converted']==92167
    args.output.mkdir(parents=True,exist_ok=False)
    source=args.output/'sources';source.mkdir()
    save(args.output/'identity.json',dict(owner=args.owner,scope='inventory_and_quality_only',training_interface='deferred_by_user'))
    for name in ['result.json','plan.json','process.json','source-provenance.json']:
        shutil.copy2(args.conversion_root/name,source/('conversion-'+name))
    frozen,provenance=snapshot_entries(args.conversion_root)
    expected={r['client']:r for r in frozen}
    entries=[];catalogs=[]
    for client_root,count in sorted(result['catalogs'].items()):
        root=Path(client_root);database=root/'episodes.sqlite3';before=sha(database)
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA quick_check').fetchall()==[('ok',)]
            values=db.execute('SELECT id,request_id,metadata,sha256,frames FROM episodes ORDER BY id').fetchall()
            templates={key:json.loads(body) for key,body in db.execute('SELECT sha256,body FROM request_templates')}
        assert len(values)==count and all(request_digest(v)==k for k,v in templates.items())
        assert sha(database)==before
        prefix=root.parent.name
        shutil.copy2(database,source/(prefix+'.sqlite3'))
        template_file=root/'request-templates.json';assert json.loads(template_file.read_text())['templates']==templates
        shutil.copy2(template_file,source/(prefix+'.request-templates.json'))
        catalogs.append(dict(client_root=client_root,count=count,sha256=before,integrity='ok',snapshot=str(source/(prefix+'.sqlite3'))))
        for cid,uid,metadata,digest,frames in values:
            old=expected.pop(str(root/cid))
            assert old['metadata']==metadata
            if old['metadata_sha256']:assert old['metadata_sha256']==digest
            entries.append(dict(case_id=cid,request_id=uid,metadata=metadata,metadata_sha256=digest,frames=frames,
                client_root=client_root,frozen_original_sha256=old['expected']))
    assert not expected and len(entries)==len({r['request_id'] for r in entries})==92167
    save(args.output/'source-snapshot.json',dict(conversion=provenance,catalogs=catalogs,
        conversion_result_sha256=sha(args.conversion_root/'result.json'),target=len(entries),
        source_split='train',training_split_assignment='unassigned',media_references_only=True))
    with gzip.open(args.output/'frozen-entries.jsonl.gz','wt') as stream:
        for entry in entries:stream.write(json.dumps(entry,separators=(',',':'))+'\n')
    started=time.time();rows=[];errors=[]
    with ProcessPoolExecutor(args.workers) as pool:
        for row in pool.map(check_episode,entries,chunksize=8):
            if 'error' in row:errors.append(row)
            else:rows.append(row)
            if (len(rows)+len(errors))%4000==0:
                progress=dict(stage='all_episode_qc',completed=len(rows),target=len(entries),errors=len(errors),elapsed_s=time.time()-started)
                save(args.output/'progress.json',progress);print(json.dumps(progress),flush=True)
    save(args.output/'structural-errors.json',errors)
    if errors:
        save(args.output/'result.json',dict(passed=False,errors=len(errors),completed=len(rows),stage='all_episode_qc'))
        raise RuntimeError('episode quality failures; see structural-errors.json')
    selected=select_deep(rows)
    save(args.output/'deep-sample-selection.json',dict(strategy='two deterministic episodes per scene, one per available target view, longest episode per scene; union',
        episodes=len(selected),uids=[r['uid'] for r in selected]))
    audits=[]
    with ProcessPoolExecutor(min(args.workers,32)) as pool:
        for audit in pool.map(audit_media,selected,chunksize=1):
            audits.append(audit)
            if len(audits)%20==0:
                save(args.output/'progress.json',dict(stage='deep_media_qc',completed=len(audits),target=len(selected),errors=sum(not r['passed'] for r in audits)))
                print('DEEP',len(audits),'/',len(selected),flush=True)
    save(args.output/'deep-media-audit.json',audits)
    bad=[a for a in audits if not a['passed']]
    if bad:
        save(args.output/'result.json',dict(passed=False,stage='deep_media_qc',errors=len(bad)))
        raise RuntimeError('deep media failures')
    audited={a['uid'] for a in audits}
    for row in rows:
        if row['uid'] in audited:row['deep_media_qc']='passed_all_frames_and_initial_views'
    finalize(args,rows,audits,catalogs,started)


def finalize(args,rows,audits,catalogs,started):
    out=args.output
    instruction=Counter(r['normalized_instruction_sha256'] for r in rows)
    duplicates=defaultdict(list);recordings=defaultdict(list)
    for row in rows:
        duplicates[row['exact_episode_fingerprint']].append(row['uid'])
        recordings[row['recording_fingerprint']].append(row['uid'])
    duplicate_groups={k:v for k,v in duplicates.items() if len(v)>1}
    recording_groups={k:v for k,v in recordings.items() if len(v)>1}
    save(out/'duplicate-groups.json',dict(exact_episode=duplicate_groups,identical_recording=recording_groups,
        policy='report only; text repetition alone is not an episode duplicate; no automatic deletion'))
    scene_groups=defaultdict(list)
    with gzip.open(out/'episodes.jsonl.gz','wt',compresslevel=3) as stream:
        for row in sorted(rows,key=lambda r:(r['scene'],r['uid'])):
            stream.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
            scene_groups[row['scene']].append(row)
    scene_rows=[]
    for scene,group in sorted(scene_groups.items()):
        scene_rows.append(dict(scene=scene,episodes=len(group),frames=sum(r['frames'] for r in group),
            deep_audited_episodes=sum(r['deep_media_qc'].startswith('passed') for r in group),
            median_path_m=float(np.median([r['path_m'] for r in group])),
            proposal_needs_review=sum('proposal_needs_review' in r['review_tags'] for r in group)))
    csv_rows(out/'scene_summary.csv',scene_rows,list(scene_rows[0]))
    fields=['uid','case_id','run','scene','source_split','split_assignment','training_admission','semantic_admission','frames','duration_s','instruction','target_view','path_m','goal_planar_m','final_error_m','technical_qc','deep_media_qc','metadata','metadata_sha256','recording_fingerprint','exact_episode_fingerprint']
    csv_rows(out/'episodes.csv',rows,fields)
    with gzip.open(out/'review-sample.jsonl.gz','wt') as stream:
        for row in rows:
            if row['deep_media_qc'].startswith('passed') or row['review_tags']:
                stream.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
    summary=dict(status='inventory_qc_complete',owner=args.owner,episodes=len(rows),scenes=len(scene_groups),
        frames=sum(r['frames'] for r in rows),control_transitions=sum(r['frames']-1 for r in rows),
        recorded_hours=sum(r['duration_s'] for r in rows)/3600,
        run_counts=dict(Counter(r['run'] for r in rows)),source_split_counts=dict(Counter(r['source_split'] for r in rows)),
        target_view_counts=dict(Counter(r['target_view'] for r in rows)),
        semantic_admission_counts=dict(Counter(r['semantic_admission'] for r in rows)),
        training_admission_counts=dict(Counter(r['training_admission'] for r in rows)),
        technical_failures=0,deep_audited_episodes=len(audits),deep_audited_frames=sum(a['frames'] for a in audits),
        deep_audited_initial_views=4*len(audits),deep_audited_bytes=sum(a['bytes_hashed_and_decoded'] for a in audits),
        deep_media_failures=0,deep_media_scope='stratified subset only; all frames of selected episodes, not all corpus media bytes',
        exact_episode_duplicate_groups=len(duplicate_groups),exact_episode_duplicate_excess=sum(len(v)-1 for v in duplicate_groups.values()),
        identical_recording_groups=len(recording_groups),unique_normalized_instructions=len(instruction),
        repeated_instruction_excess=sum(v-1 for v in instruction.values()),
        review_tag_counts=dict(Counter(tag for r in rows for tag in r['review_tags'])),
        camera_configs=dict(Counter(json.dumps(r['camera'],sort_keys=True) for r in rows)),
        available_observations=dict(initial_views=['front','right','back','left'],sequence_views=['front'],control_dt_s=.25,
            initial_views_are_not_per_step_multiview=True),
        training_interface='deferred_by_user',new_train_dev_split='not_assigned',training_examples_exported=0,
        training_ready=False,semantic_review='pending; structural/media checks do not certify instruction correctness',
        elapsed_s=time.time()-started)
    save(out/'summary.json',summary)
    write_report(out,summary)
    outputs={p.name:sha(p) for p in out.iterdir() if p.is_file() and p.name not in ['progress.json','manifest.json','result.json']}
    save(out/'manifest.json',dict(schema='nav_train.free_collection_inventory.v1',owner=args.owner,
        scope='inventory_and_quality_only',source_snapshot_sha256=sha(out/'source-snapshot.json'),files_sha256=outputs,
        producer_sha256=sha(__file__),training_interface='deferred_by_user',training_ready=False,
        output=str(out),created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()))
    result=dict(passed=True,status='inventory_qc_complete',episodes=len(rows),scenes=len(scene_groups),
        structural_failures=0,deep_media_failures=0,manifest_sha256=sha(out/'manifest.json'),training_ready=False,
        source_data_modified=False,training_examples_exported=0)
    save(out/'result.json',result)
    save(out/'progress.json',{**result,'status':'completed'})
    return summary


def write_report(out,s):
    text=f'''# 自由采集数据：清单与质量核验

范围：{s['episodes']:,}条成功episode、{s['scenes']}个train场景，{s['frames']:,}个实际前视RGB-D帧，录制约{s['recorded_hours']:.2f}小时。

本轮按用户选择只做清单和质量检查；未决定样本接口，未分配新的train/dev比例，未生成训练窗口或启动训练。所有源数据和原有审核状态保持不变。

## 全量检查

五个最终SQLite索引只读完整性检查、计数及文件SHA256冻结；全部episode与转换前冻结来源清单对齐。逐条核对meta/catalog哈希、来源身份、train来源、成功回执、帧数、4Hz时间轴、位姿和动作逐步对齐、PointNav固定目标与逐帧变换、真实STOP、零碰撞、RGB/深度完整文件集合及四张起点图存在性。结构失败：{s['technical_failures']}。

## 媒体深度抽检

抽取{s['deep_audited_episodes']:,}条episode，覆盖所有场景及可用目标方向，并包含每场景最长录制；逐帧核对SHA256、实际解码RGB、读取深度数组并检查形状/float32/有限值/有效深度。抽检{s['deep_audited_frames']:,}对RGB-D帧、{s['deep_audited_initial_views']:,}张起点图，共{s['deep_audited_bytes']/1024**3:.2f}GiB，失败{s['deep_media_failures']}。这不是对全量媒体逐字节重新审计。

## 实际可用输入

每条任务起点有前/右/后/左四视图；执行过程只有前视RGB-D序列、实际本体/传感器位姿、控制动作和终止回执。起点侧视图不能标作后续时刻的当前侧视。具体训练输入、历史选择、输出动作/轨迹协议留待讨论。

## 重复和语义

按实际录制内容、标准化指令和固定目标识别完全重复episode：{s['exact_episode_duplicate_groups']}组，额外重复{s['exact_episode_duplicate_excess']}条；只报告，不自动删除。标准化后{s['unique_normalized_instructions']:,}种指令，重复文本额外{s['repeated_instruction_excess']:,}条不等于重复轨迹。语义状态仍为pending_review；几何成功和媒体完整不代表指令已人工验收。

## 文件

- episodes.jsonl.gz / episodes.csv：逐条清单，包含源meta路径与哈希、场景、指令、起终点、观察能力、帧数、质量结果和分组指纹。
- source-snapshot.json、sources/：转换完成凭证和五个catalog、共享请求模板的冻结副本。
- scene_summary.csv：逐场景数量与检查覆盖；duplicate-groups.json：重复分组。
- deep-sample-selection.json、deep-media-audit.json：抽样规则、身份和逐条深度审计。
- review-sample.jsonl.gz：抽检及带复核标签的记录引用，待语义抽检，不代表审核通过。
- manifest.json、result.json：输出校验和与最终状态。split_assignment保持unassigned，training_ready保持false。
'''
    (out/'report.md').write_text(text)


