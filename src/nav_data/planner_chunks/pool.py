"""Shared annotation queue with explicit per-server in-flight limits.

Uses planner_chunk_annotation.annotate for the sole two-stage model loop.
No service restarts, movement, automatic retries, or training admission.
"""
import ipaddress
import json
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from nav_data.collection.pool import ModelPool
from nav_data.planner_chunks.annotation import (
    annotate,
    digest,
    expand_media,
    validate_cases,
    write_new,
)


def run_queue(cases, profiles, protocol, media, out, repeats=1, worker=annotate):
    validate_cases(cases)
    if repeats < 1 or not profiles:
        raise ValueError('positive repeats and profiles required')
    results = []
    lock = threading.Lock()
    start = time.monotonic()

    def consume(job, profile, enqueued):
        index, case = job
        config = profile['config']
        result = worker(case, config, protocol, media, out / f'job-{index:05d}', profile['endpoint'])
        result = dict(result, server_profile=profile['id'])
        write_new(out / f'job-{index:05d}' / 'assignment.json',
                  {'profile': profile['id'], 'job_index': index, 'case_id': case['id']})
        with lock:
            results.append(result)
            print(json.dumps({'done': len(results), 'total': len(cases) * repeats,
                              'server': profile['id'], 'status': result['status'],
                              'wall_s': round(result['wall_s'], 2)}), flush=True)

    pool = ModelPool(profiles, max(1, sum(p['slots'] for p in profiles)), consume, strict=False).start()
    try:
        for index in range(len(cases)*repeats):
            pool.submit((index, cases[index % len(cases)]))
        pool.close(progress_timeout=max((p['config'].get('request_timeout_s', 180) for p in profiles), default=180)*2+5)
    except BaseException:
        pool.close(cancel=True)
        raise
    pool.check()
    wall = time.monotonic() - start
    completed = [r for r in results if r['status'] == 'completed_audit']
    per_server = {}
    for p in profiles:
        rows = [r for r in results if r['server_profile'] == p['id']]
        per_server[p['id']] = {'slots': p['slots'], 'jobs': len(rows),
                               'completed': sum(r['status'] == 'completed_audit' for r in rows),
                               'failed_or_uncertain': sum(r['status'] != 'completed_audit' for r in rows),
                               'job_wall_s': [r['wall_s'] for r in rows]}
    summary = {'jobs': len(results), 'unique_cases': len(cases), 'completed_audits': len(completed),
               'failed_or_uncertain': len(results) - len(completed), 'wall_s': wall,
               'completed_routes_per_s': len(completed) / wall,
               'total_inflight_limit': sum(p['slots'] for p in profiles), 'per_server': per_server,
               'admission': 'audit_only', 'movement_steps': 0, 'training_exports': 0,
               'concurrency': pool.stats()}
    write_new(out / 'summary.json', summary)
    return summary


@contextmanager
def forwarded_profiles(config, out):
    """Own one temporary SSH tunnel; only this subprocess is stopped on exit."""
    held, profiles, forwards = [], [], []
    try:
        for p in config['profiles']:
            u = urlsplit(p['endpoint'])
            if (u.scheme != 'http' or not u.hostname or not u.port or u.username or u.password
                    or not ipaddress.ip_address(u.hostname).is_private):
                raise ValueError('configured private-IP HTTP endpoints required')
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            held.append(s)
            port = s.getsockname()[1]
            forwards.append(f'127.0.0.1:{port}:{u.hostname}:{u.port}')
            profiles.append(dict(p, endpoint=f'http://127.0.0.1:{port}{u.path}'))
        owner = 'navtrain-weighted-pool-' + uuid.uuid4().hex
        argv = ['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'ServerAliveInterval=15']
        for f in forwards:
            argv.extend(['-L', f])
        argv.append(config['ssh_host'])
        write_new(out / 'ownership.json', {'owner': owner, 'argv': argv, 'server_process_control': False})
    finally:
        for s in held:
            s.close()
    with (out / 'tunnel.stderr.log').open('x') as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
        write_new(out / 'tunnel-started.json', {'owner': owner, 'pid': proc.pid, 'time': time.time()})
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError('owned tunnel failed; see stderr')
                try:
                    for p in profiles:
                        with socket.create_connection(('127.0.0.1', urlsplit(p['endpoint']).port), timeout=.1):
                            pass
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise TimeoutError('owned tunnel readiness timeout')
            write_new(out / 'forwarded-profiles.json', profiles)
            yield profiles
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            write_new(out / 'tunnel-stopped.json', {'owner': owner, 'pid': proc.pid,
                       'returncode': proc.returncode, 'time': time.time(), 'only_owned_process_stopped': True})


def run_pool(args):
    paths = list(map(Path, (args.cases, args.config, args.protocol)))
    cases, config, protocol = [json.loads(p.read_text()) for p in paths]
    validate_cases(cases)
    if sum(p['slots'] for p in config['profiles']) > config['max_total_inflight']:
        raise ValueError('profile slots exceed configured total')
    media = paths[0].parent / 'media'
    for c in cases:
        expand_media(c['request'], media)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / 'manifest.json', {'config': config, 'protocol': protocol, 'repeats': args.repeats,
              'source_sha256': {str(p): digest(p) for p in paths + [Path(__file__),
                              Path(__file__).with_name('annotation.py'),
                              Path(__file__).parents[1]/'collection/pool.py',
                              Path(__file__).parents[1]/'records.py']},
              'media_sha256': {p.name: digest(p) for p in media.iterdir() if p.is_file()},
              'audit_only': True, 'full_dataset_launched': False})
    with forwarded_profiles(config, out) as profiles:
        result = run_queue(cases, profiles, protocol, media, out, args.repeats)
    return result

