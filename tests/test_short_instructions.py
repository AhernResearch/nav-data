import copy
import json
from types import SimpleNamespace

import pytest

from nav_data.tasks import short_run as short
from nav_data.tasks.policy import validate_distance_limits, visible_region_config


def profile():
    return SimpleNamespace(dt_s=.25,v_max_mps=.6,omega_max_rps=1.26)


def source():
    return {'owner':'old','profile':{'model':'test','endpoint':'http://localhost:18110/v1/chat/completions','api_key':'do-not-copy'},
        'max_local_geodesic_m':8.,'cases':[{'id':'old-right','target_view':'right',
            'episode':{'_dataset':'train_pilot'},'proposal_source':'old','proposal_sha256':'old',
            'comparison_observation_source':'old-demo','manual_review':{'approved':True}}]}


def test_new_owned_visible_box_config_drops_legacy_distance_camera_and_reused_proposal_constraints():
    original=source();before=copy.deepcopy(original);config=visible_region_config(original)
    assert original==before and config['owner']!='old'
    assert config['target_mode']=='box' and config['max_local_geodesic_m'] is None
    assert config['min_local_geodesic_m']==0 and not config['camera_coverage_conditioned']
    item=config['cases'][0]
    assert 'target_view' not in item and item['source_coverage_view']=='right'
    assert not {'proposal_source','proposal_sha256','manual_review','comparison_observation_source'}&item.keys()
    assert 'api_key' not in config['profile']


def test_visible_profile_rejects_evaluation_sources_and_embedded_credentials():
    value=source();value['cases'][0]['episode']['_dataset']='val_unseen'
    with pytest.raises(ValueError,match='train source'):visible_region_config(value)
    value=source();value['profile']['endpoint']='http://secret@localhost/v1'
    with pytest.raises(ValueError,match='credentials'):visible_region_config(value)
    value=source();value['backend_manifest']={'password':'secret'}
    with pytest.raises(ValueError,match='credentials'):visible_region_config(value)


def test_short_config_accepts_the_r2r_concurrency_fields_without_legacy_gpu_or_profile():
    value=source();profile=value.pop('profile')
    value.update(simulation_workers=[{'id':'w0','gpu':0},{'id':'w1','gpu':3}],
                 model_profiles=[{'id':'m0','model':profile['model'],'endpoint':profile['endpoint'],'slots':4}],
                 ready_queue_size=3,candidate_timeout_s=30,request_timeout_s=10)
    config=visible_region_config(value)
    assert config['simulation_workers']==value['simulation_workers']
    assert config['model_profiles']==value['model_profiles']
    assert 'profile' not in config and 'gpu' not in config


@pytest.mark.parametrize('minimum,maximum',[(-1,None),(0,float('inf')),(0,float('nan')),(2,1),(False,None)])
def test_invalid_distance_configuration_cannot_silently_disable_checks(minimum,maximum):
    with pytest.raises(ValueError):validate_distance_limits(minimum,maximum)


def test_prepare_freezes_config_and_source_without_launching(tmp_path, monkeypatch):
    source_path = tmp_path / 'source.json'
    source_path.write_text(json.dumps(source()))
    output = tmp_path / 'prepared'
    result = short.prepare_short(source_path, output)
    provenance = json.loads((output / 'source-provenance.json').read_text())
    assert result['cases'] == 1 and provenance['status'] == 'prepared_not_launched'
    assert provenance['model_requests'] == provenance['movement_commands'] == 0
    assert 'src/nav_data/tasks/short_run.py' in provenance['source_sha256']
    assert not (output / 'started.json').exists()
    with pytest.raises(ValueError, match='collection_service'):
        short.collect_short(output)
    snapshot = output / 'source/src/nav_data/records.py'
    snapshot.write_text(snapshot.read_text() + '# changed\n')
    with pytest.raises(ValueError, match='source changed'):
        short.collect_short(output)


def test_collect_submits_the_frozen_cases_to_the_service_only(tmp_path, monkeypatch):
    scenes = tmp_path / 'scenes'
    scenes.mkdir()
    value = source()
    value.pop('profile')
    value.update(simulation_workers=[{'id': 'w0', 'gpu': 0}], ready_queue_size=2,
                 model_profiles=[{'id': 'm0', 'model': 'test', 'endpoint': 'http://localhost/v1', 'slots': 1}],
                 collection_service={'url': 'http://fake', 'shared_root': str(tmp_path / 'shared'),
                                     'dataset': 'train_pilot', 'camera': 'camera'},
                 backend_manifest={'config': {'split': 'train', 'goal_tolerance_m': .12},
                                   'runtime': {'settings': {'simulation': {'scenes_root': str(scenes)}}}})
    value['cases'][0]['episode']['scene_id'] = 'room.glb'
    source_path = tmp_path / 'source.json'
    source_path.write_text(json.dumps(value))
    output = tmp_path / 'prepared'
    short.prepare_short(source_path, output)
    seen = {}

    def fake(manifest, root, kind, service, **options):
        seen.update(manifest=manifest, root=root, kind=kind, service=service, options=options)
        return {'counts': {'completed_audit': 1}}

    monkeypatch.setattr(short, 'run_remote', fake)
    assert short.collect_short(output) == {'counts': {'completed_audit': 1}}
    assert seen['kind'] == 'short_instructions' and seen['options'] == {'prepared_output': True}
    assert [c['id'] for c in seen['manifest']['candidates']] == ['old-right']
    assert seen['manifest']['candidates'][0]['scene'] == str((scenes / 'room.glb').resolve())
