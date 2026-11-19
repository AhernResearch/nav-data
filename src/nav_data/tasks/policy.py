"""Visible-target collection policy, independent of movement and model transport."""
import copy
import math
import uuid
from urllib.parse import urlsplit

from nav_data.collection.pool import validate_concurrency


def short_concurrency(config):
    """Same scheduling fields as R2R; old single-profile configurations remain valid."""
    backend=config.get('backend_manifest',{}).get('config',{})
    profiles=config.get('model_profiles')
    if profiles is None:
        profile={key:config['profile'][key] for key in ('model','endpoint')}
        profiles=[dict(profile,id='model-0',slots=1)]
    settings={
        'simulation_workers':copy.deepcopy(config.get('simulation_workers',[{'id':'sim-0','gpu':config.get('gpu',0)}])),
        'model_profiles':copy.deepcopy(profiles),
        'ready_queue_size':config.get('ready_queue_size',backend.get('ready_queue_size',32)),
        'candidate_timeout_s':config.get('candidate_timeout_s',backend.get('candidate_timeout_s',600)),
        'request_timeout_s':config.get('request_timeout_s',backend.get('request_timeout_s',180)),
        'minimum_free_disk_bytes':config.get('minimum_free_disk_bytes',backend.get('minimum_free_disk_bytes',0)),
    }
    validate_concurrency(settings)
    return settings


def validate_distance_limits(min_distance=0.,max_distance=None):
    if (type(min_distance) not in (int,float) or not math.isfinite(min_distance) or min_distance<0
            or (max_distance is not None and (type(max_distance) not in (int,float)
                or not math.isfinite(max_distance) or max_distance<=min_distance))):
        raise ValueError('invalid target distance limits')


def visible_region_config(source):
    """Prepare a new configuration; existing configurations and attempts stay immutable."""
    config=copy.deepcopy(source)
    if 'profile' in source:
        profile=source['profile'];endpoint=urlsplit(profile['endpoint'])
        if endpoint.username or endpoint.password or endpoint.query:
            raise ValueError('credentials and endpoint query parameters must stay outside snapshots')
        config['profile']={key:profile[key] for key in ['model','endpoint']}
    config.update(short_concurrency(config))
    config.update(owner=uuid.uuid4().hex,purpose='model_defined_visible_region_tasks',
        target_mode='box',target_scope='current_visible_floor',min_local_geodesic_m=0.,
        max_local_geodesic_m=None,camera_coverage_conditioned=False,automatic_retry=False,admission='audit_only',
        execution_budget_policy={'mode':'path_scaled','time_multiplier':3.,'settle_seconds':10.})
    config['visible_floor_policy']={'enabled':True,'horizontal_limit':.01,'vertical_limit':.2,
                                  'slope_degrees':25.,'adjustment_support_tolerance_m':.025}
    config.setdefault('region_grid_size',13);config.setdefault('region_plan_candidates',12)
    config.setdefault('region_adjustment_radius_m',.15);config.setdefault('region_max_previews',64)
    config.setdefault('region_adjustments_per_candidate',4)
    for key in ['prior_experiment','comparison_baselines','selection_policy']:
        config.pop(key,None)
    for item in config['cases']:
        from nav_data.tasks.pointnav import require_train
        if 'config' in config.get('backend_manifest', {}):
            require_train(config, item)
        elif item['episode'].get('_dataset') != 'train_pilot':
            raise ValueError('visible collection requires train source or registered GS development source')
        if 'target_view' in item:item['source_coverage_view']=item.pop('target_view')
        for key in ['proposal_source','proposal_sha256','manual_review','comparison_observation_source']:item.pop(key,None)
    def inspect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key.lower() in {'api_key','password','secret','token','access_token','authorization','client_secret'} and item:
                    raise ValueError('credentials must stay outside experiment snapshots')
                inspect(item)
        elif isinstance(value,list):
            for item in value:inspect(item)
    inspect(config)
    return config
