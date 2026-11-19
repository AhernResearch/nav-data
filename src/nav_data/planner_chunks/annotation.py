"""Free planning followed by frozen-plan chunk alignment; audit output only.

No environment execution, automatic retry, training export, or service control.
Run as ``python -m nav_data.planner_chunks.annotation --help``.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import shlex
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import jsonschema


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def expand_media(value, media_root):
    if isinstance(value, dict) and 'artifact' in value:
        key = value['artifact']
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid media digest')
        raw = (media_root / key).read_bytes()
        if hashlib.sha256(raw).hexdigest() != key:
            raise ValueError('media digest mismatch')
        return 'data:' + value['mime_type'] + ';base64,' + base64.b64encode(raw).decode()
    if isinstance(value, dict):
        return {k: expand_media(v, media_root) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_media(v, media_root) for v in value]
    return value


def validate_cases(cases):
    """Validate the entire input before scheduling any calls (including repeats)."""
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('cases must be nonempty with unique IDs')
    forbidden = {'reference_path', 'goals', 'goal_position', 'evaluation_metrics',
                 'success', 'spl', 'geodesic_distance'}

    def inspect(value):
        if isinstance(value, dict):
            if forbidden.intersection(value):
                raise ValueError('privileged structured input')
            for v in value.values():
                inspect(v)
        elif isinstance(value, list):
            for v in value:
                inspect(v)

    for case in cases:
        if case.get('split') != 'train':
            raise ValueError('only train-source audit cases allowed')
        if not case.get('chunks') or any(not isinstance(c, str) or not c.strip() for c in case['chunks']):
            raise ValueError('nonempty chunk texts required')
        request = case['request']
        inspect(request)
        names = [t['function']['name'] for t in request['tools']]
        if names.count('update_plan') != 1:
            raise ValueError('one update_plan schema required')
        # Caller must provide a reviewed, pre-plan model-visible observation request.
        if any(c.get('function', {}).get('name') == 'update_plan'
               for m in request['messages'] for c in m.get('tool_calls', [])):
            raise ValueError('stage one must not contain a previously generated plan')


def parsed_call(response, tool):
    choice = response['choices'][0]
    if choice.get('finish_reason') not in ('stop', 'tool_calls'):
        raise ValueError('incomplete model output')
    message = choice['message']
    calls = message.get('tool_calls', [])
    if len(calls) != 1 or calls[0]['function']['name'] != tool['function']['name']:
        raise ValueError('expected exactly the requested annotation tool')
    args = json.loads(calls[0]['function']['arguments'])
    jsonschema.validate(args, tool['function']['parameters'])
    return args, {k: message[k] for k in ('role', 'content', 'tool_calls') if k in message}


def wire_request(request, config):
    """Profile-selected response encoding; model names never select behavior."""
    mode = config.get('response_mode', 'tool_call')
    if mode == 'tool_call':
        return copy.deepcopy(request)
    if mode != 'json_schema':
        raise ValueError('unknown response_mode')
    target = request['tool_choice']['function']['name']
    tool = next(t for t in request['tools'] if t['function']['name'] == target)
    q = copy.deepcopy(request)
    for key in ('tools', 'tool_choice', 'parallel_tool_calls'):
        q.pop(key, None)
    q['response_format'] = {'type': 'json_schema', 'json_schema': {
        'name': target, 'strict': True, 'schema': tool['function']['parameters']}}
    q['messages'].append({'role': 'user', 'content':
        'For this offline annotation response, return only the JSON argument object for '
        + target + '. The supplied response schema defines its fields. Do not emit a tool-call wrapper. '
        'Do not execute any action. All preceding task and frozen-plan constraints still apply.'})
    return q


def tool_response(response, target, config):
    """Normalize JSON response locally; raw wire response is preserved separately."""
    if config.get('response_mode', 'tool_call') == 'tool_call':
        return response
    result = copy.deepcopy(response)
    msg = result['choices'][0]['message']
    value = json.loads(msg['content'])
    msg['content'] = None
    msg['tool_calls'] = [{'id': 'offline-json-' + target, 'type': 'function',
                         'function': {'name': target, 'arguments': json.dumps(value)}}]
    return result


def alignment_request(request, plan, message, chunks, protocol, max_tokens):
    q = copy.deepcopy(request)
    q['messages'].append(copy.deepcopy(message))
    q['messages'].append({'role': 'tool', 'tool_call_id': message['tool_calls'][0]['id'],
                          'content': json.dumps({'recorded_plan': plan['subtasks'],
                                                 'movement_executed': False, 'annotation_only': True})})
    q['messages'].append({'role': 'user', 'content': protocol['alignment_prompt'] + json.dumps(
        [{'chunk_id': i, 'text': c} for i, c in enumerate(chunks)])})
    q['tools'].append(copy.deepcopy(protocol['alignment_tool']))
    q['tool_choice'] = {'type': 'function', 'function': {'name': 'record_chunk_alignment'}}
    q['max_tokens'] = max_tokens
    return q


def validate_chunk_partition(plan, chunks):
    """Whole-chunk plans must partition the source exactly once, in order."""
    flattened = []
    for task in plan['subtasks']:
        ids = task.get('chunk_ids')
        if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
            raise ValueError('each task requires nonempty integer chunk_ids')
        flattened.extend(ids)
    if flattened != list(range(len(chunks))):
        raise ValueError('plan must partition all chunks exactly once in source order')


def review_alignment(plan, alignment, chunks):
    """Check structure; flag partial/overlapping actions without rejecting shared endpoints."""
    tasks = plan['subtasks']
    if not tasks or len({t['id'] for t in tasks}) != len(tasks):
        raise ValueError('plan IDs must be nonempty and unique')
    rows = alignment['alignments']
    if [a['task_id'] for a in rows] != [t['id'] for t in tasks]:
        raise ValueError('alignment must preserve task IDs and order')
    flags = {a['task_id']: [] for a in rows}
    action_sets = {}
    for a in rows:
        ids = a['chunk_ids']
        if any(type(i) is not int or i < 0 or i >= len(chunks) for i in ids):
            raise ValueError('out-of-range chunk')
        if ids and ids != list(range(ids[0], ids[-1] + 1)):
            raise ValueError('selected chunks must be contiguous and unique')
        if [c['chunk_id'] for c in a['coverage']] != ids:
            raise ValueError('coverage IDs mismatch')
        action_sets[a['task_id']] = {c['chunk_id'] for c in a['coverage']
                                     if c['relation'] in ('full_action', 'partial_action')}
        if not ids:
            flags[a['task_id']].append('no_match')
        if any(c['relation'] == 'partial_action' for c in a['coverage']):
            flags[a['task_id']].append('partial_action_boundary_review')
        if a['full_chunk_interval_reusable'] and (not ids or any(
                c['relation'] != 'full_action' for c in a['coverage'])):
            flags[a['task_id']].append('reusable_claim_conflicts_with_coverage')
        if a['needs_review']:
            flags[a['task_id']].append('model_requests_review')
    for tid, ids in action_sets.items():
        if any(tid != other and ids & other_ids for other, other_ids in action_sets.items()):
            flags[tid].append('overlapping_action_claims')
    uncovered = alignment['uncovered_action_chunk_ids']
    if any(type(i) is not int or not 0 <= i < len(chunks) for i in uncovered):
        raise ValueError('invalid uncovered chunk ID')
    if len(set(uncovered)) != len(uncovered):
        raise ValueError('duplicate uncovered chunk ID')
    return flags


def http_call(endpoint, payload, config):
    body = json.dumps(payload)
    timeout = config.get('request_timeout_s', 180)
    if config.get('ssh_host'):
        code = ("import sys,json,urllib.request; p=json.load(sys.stdin); "
                f"r=urllib.request.urlopen(urllib.request.Request({endpoint!r}, "
                "data=json.dumps(p).encode(),headers={'Content-Type':'application/json'}),"
                f"timeout={int(timeout)}); sys.stdout.write(r.read().decode())")
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 config['ssh_host'], 'python3 -c ' + shlex.quote(code)],
                                input=body, text=True, capture_output=True, timeout=timeout + 20)
        if result.returncode:
            raise RuntimeError(result.stderr[:1000])
        return json.loads(result.stdout)
    req = urllib.request.Request(endpoint, data=body.encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def annotate(case, config, protocol, media_root, folder, endpoint, caller=http_call):
    folder.mkdir()
    result = {'case_id': case['id'], 'split': case['split'], 'admission': 'audit_only',
              'endpoint': endpoint, 'stages': [], 'movement_steps': 0, 'training_exports': 0}
    start = time.monotonic()
    try:
        q = copy.deepcopy(case['request'])
        q.update(model=config['model'], temperature=config.get('temperature', 0),
                 max_tokens=config.get('plan_max_tokens', 2048),
                 chat_template_kwargs={'enable_thinking': config.get('thinking', False)},
                 tool_choice={'type': 'function', 'function': {'name': 'update_plan'}})
        for stage in ('plan', 'alignment'):
            write_new(folder / (stage + '.request.json'), q)
            write_new(folder / (stage + '.started.json'), {'wall_time': time.time(), 'endpoint': endpoint})
            wire = wire_request(q, config)
            write_new(folder / (stage + '.wire-request.json'), wire)
            t = time.monotonic()
            response = caller(endpoint, expand_media(wire, media_root), config)
            wall = time.monotonic() - t
            write_new(folder / (stage + '.response.json'), response)
            target = 'update_plan' if stage == 'plan' else 'record_chunk_alignment'
            tool = next(t for t in q['tools'] if t['function']['name'] == target)
            value, message = parsed_call(tool_response(response, target, config), tool)
            result['stages'].append({'stage': stage, 'wall_s': wall, 'usage': response.get('usage')})
            if stage == 'plan':
                plan = value
                if not plan['subtasks'] or len({t['id'] for t in plan['subtasks']}) != len(plan['subtasks']):
                    raise ValueError('invalid plan task IDs')
                if case.get('plan_constraint') == 'whole_chunks':
                    validate_chunk_partition(plan, case['chunks'])
                write_new(folder / 'frozen_plan.json', plan)
                q = alignment_request(q, plan, message, case['chunks'], protocol,
                                      config.get('alignment_max_tokens', 4096))
            else:
                flags = review_alignment(plan, value, case['chunks'])
                if case.get('plan_constraint') == 'whole_chunks':
                    if any(t['chunk_ids'] != a['chunk_ids'] for t, a in zip(
                            plan['subtasks'], value['alignments'])):
                        raise ValueError('audit must preserve frozen chunk assignments')
                result.update(plan=plan, alignment=value, review_flags=flags)
        result['status'] = 'completed_audit'
    except Exception as error:
        # Started records survive; a timed-out request may still be running server-side.
        result.update(status='failed_or_uncertain', error=str(error), automatic_retry=False)
    result['wall_s'] = time.monotonic() - start
    write_new(folder / 'result.json', result)
    return result


def run(args):
    cases_path, config_path, protocol_path = map(Path, (args.cases, args.config, args.protocol))
    cases, config, protocol = [json.loads(p.read_text()) for p in (cases_path, config_path, protocol_path)]
    validate_cases(cases)
    if not 1 <= args.concurrency <= config.get('max_concurrency', 12) or args.repeats < 1:
        raise ValueError('invalid bounded concurrency or repeats')
    if not config['endpoints']:
        raise ValueError('endpoint list required')
    media = cases_path.parent / 'media'
    for c in cases:
        expand_media(c['request'], media)  # verify all media before any request
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / 'manifest.json', {'config': config, 'protocol': protocol, 'concurrency': args.concurrency,
              'repeats': args.repeats, 'cases': len(cases), 'admission': 'audit_only',
              'source_sha256': {str(p): digest(p) for p in (cases_path, config_path, protocol_path, Path(__file__))},
              'media_sha256': {p.name: digest(p) for p in media.iterdir() if p.is_file()},
              'caveat': 'Repeated four-route fixtures are cache-sensitive, not a representative full-data benchmark.'})
    start = time.monotonic()
    jobs = [(i, c) for i, c in enumerate(cases * args.repeats)]
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(annotate, c, config, protocol, media, out / f'job-{i:05d}',
                               config['endpoints'][i % len(config['endpoints'])]) for i, c in jobs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({'done': len(results), 'total': len(jobs), 'status': result['status'],
                              'wall_s': round(result['wall_s'], 2)}), flush=True)
    wall = time.monotonic() - start
    successes = [r for r in results if r['status'] == 'completed_audit']
    summary = {'jobs': len(jobs), 'unique_cases': len(cases), 'completed_audits': len(successes),
               'failed_or_uncertain': len(results) - len(successes), 'concurrency': args.concurrency,
               'wall_s': wall, 'completed_routes_per_s': len(successes) / wall,
               'stage_wall_s': {s: [v['wall_s'] for r in results for v in r['stages'] if v['stage'] == s]
                                for s in ('plan', 'alignment')},
               'review_flagged_tasks': sum(bool(f) for r in successes for f in r['review_flags'].values()),
               'admission': 'audit_only', 'training_exports': 0, 'movement_steps': 0}
    write_new(out / 'summary.json', summary)
    print(json.dumps(summary), flush=True)
