import json

from test_compact_release import example, save_case_templates

from nav_data.release import compact as compact
from nav_data.release import inventory


def prepared(tmp_path):
    case=example(tmp_path)
    client,shared,raw,_=case
    save_case_templates([case])
    compact.convert_episode_directory((client,shared,raw/'meta.json'),full=True)
    data=compact.read(raw/'meta.json')
    entry=dict(metadata=str(raw/'meta.json'),metadata_sha256=inventory.sha(raw/'meta.json'),
        request_id=data['request_id'],case_id=data['id'],client_root=str(client.parent),
        frames=data['frames'],frozen_original_sha256={})
    return raw,entry


def test_full_checks_keep_review_pending_and_do_not_select_training_format(tmp_path):
    raw,entry=prepared(tmp_path)
    row=inventory.check_episode(entry)
    assert 'error' not in row,row
    assert row['split_assignment']=='unassigned'
    assert row['training_admission']==row['semantic_admission']=='pending_review'
    assert row['sequence_views']==['front']
    assert set(row['initial_views'])=={'front','right','back','left'}
    assert 'trajectory_label' not in row
    assert inventory.audit_media(row)['passed']


def test_changed_metadata_is_rejected_against_frozen_catalog(tmp_path):
    raw,entry=prepared(tmp_path)
    with (raw/'meta.json').open('a') as stream:stream.write(' ')
    assert 'hash mismatch' in inventory.check_episode(entry)['error']


def test_bad_frame_action_alignment_is_rejected_even_with_new_metadata_hash(tmp_path):
    raw,entry=prepared(tmp_path)
    data=compact.read(raw/'meta.json')
    data['commands'][0]['step']=100
    (raw/'meta.json').write_text(json.dumps(data));entry['metadata_sha256']=inventory.sha(raw/'meta.json')
    assert 'command/state mismatch' in inventory.check_episode(entry)['error']


def test_deep_media_catches_corruption_beyond_filename_checks(tmp_path):
    raw,entry=prepared(tmp_path)
    (raw/'rgb/00003.jpg').write_bytes(b'not a jpeg')
    row=inventory.check_episode(entry)
    assert 'error' not in row
    result=inventory.audit_media(row)
    assert result['passed'] is False and 'hash mismatch' in result['error']


def test_deep_selection_covers_scenes_views_and_longest_episode():
    rows=[dict(uid=f'{scene}-{view}-{i}',scene=scene,target_view=view,frames=i+10)
          for scene in ['a','b'] for view in ['front','right','back','left'] for i in range(5)]
    rows.append(dict(uid='longest',scene='a',target_view='front',frames=100))
    selected=inventory.select_deep(rows)
    assert {r['uid'] for r in selected}=={r['uid'] for r in inventory.select_deep(list(reversed(rows)))}
    assert 'longest' in {r['uid'] for r in selected}
    assert {(r['scene'],r['target_view']) for r in selected}=={(s,v) for s in ['a','b'] for v in ['front','right','back','left']}
