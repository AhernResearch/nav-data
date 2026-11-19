import copy
import json
from pathlib import Path

import pytest

from nav_data.planner_chunks.annotation import (
    annotate,
    review_alignment,
    validate_cases,
    validate_chunk_partition,
    wire_request,
    write_new,
)

ROOT = Path(__file__).resolve().parents[1] / 'annotations/planner_chunks'


def fixture():
    return json.loads((ROOT / 'fixtures/cases.json').read_text())[0]


def row(tid, ids, relations=None):
    relations = relations or ['full_action'] * len(ids)
    return {'task_id': tid, 'chunk_ids': ids,
            'coverage': [{'chunk_id': i, 'relation': rel, 'explanation': 'test'}
                         for i, rel in zip(ids, relations)],
            'full_chunk_interval_reusable': True, 'added_details': [],
            'needs_review': False, 'explanation': 'test'}


def plan():
    return {'subtasks': [{'id': 1, 'instruction': 'Descend.', 'status': 'pending'},
                         {'id': 2, 'instruction': 'Stop and wait.', 'status': 'pending'}],
            'reason': 'test'}


def response(name, value):
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(value)}}]}}]}


def test_combined_chunks_and_shared_endpoint_are_distinct():
    a = {'alignments': [row(1, [0, 1], ['full_action', 'endpoint_context']), row(2, [1, 2])],
         'uncovered_action_chunk_ids': [], 'overall_note': ''}
    flags = review_alignment(plan(), a, ['descend', 'stop', 'wait'])
    assert 'overlapping_action_claims' not in flags[1]
    assert 'reusable_claim_conflicts_with_coverage' in flags[1]
    assert flags[2] == []


def test_overlapping_full_and_partial_action_requires_review():
    a = {'alignments': [row(1, [0]), row(2, [0], ['partial_action'])],
         'uncovered_action_chunk_ids': [], 'overall_note': ''}
    flags = review_alignment(plan(), a, ['around island'])
    assert all('overlapping_action_claims' in f for f in flags.values())
    assert 'partial_action_boundary_review' in flags[2]


@pytest.mark.parametrize('ids', [[1, 0], [0, 2], [0, 0], [-1], [3], [True]])
def test_invalid_chunk_intervals(ids):
    a = {'alignments': [row(1, ids), row(2, [1])], 'uncovered_action_chunk_ids': []}
    with pytest.raises(ValueError):
        review_alignment(plan(), a, ['a', 'b', 'c'])


def test_evaluation_split_and_privileged_input_rejected():
    c = fixture()
    validate_cases([c])
    bad = copy.deepcopy(c)
    bad['split'] = 'val_unseen'
    with pytest.raises(ValueError):
        validate_cases([bad])
    bad = copy.deepcopy(c)
    bad['request']['reference_path'] = [[0, 0, 0]]
    with pytest.raises(ValueError):
        validate_cases([bad])


def test_two_stage_freeze_and_no_movement(tmp_path):
    c = fixture()
    protocol = json.loads((ROOT / 'protocol.v1.json').read_text())
    p = plan()
    a = {'alignments': [row(1, [0]), row(2, [1])],
         'uncovered_action_chunk_ids': [], 'overall_note': 'test'}
    payloads = []

    def call(endpoint, payload, config):
        payloads.append(payload)
        if len(payloads) == 1:
            assert payload['messages'] == c['request']['messages'] or len(payload['messages']) == len(c['request']['messages'])
            return response('update_plan', p)
        return response('record_chunk_alignment', a)

    folder = tmp_path / 'attempt'
    result = annotate(c, {'model': 'test'}, protocol, ROOT / 'fixtures/media', folder, 'unused', call)
    assert result['status'] == 'completed_audit'
    assert result['plan'] == p
    assert len(payloads) == 2
    assert json.loads((folder / 'frozen_plan.json').read_text()) == p
    # Stage one has no chunk augmentation; stage two adds metadata only after the frozen plan.
    q1 = json.loads((folder / 'plan.request.json').read_text())
    q2 = json.loads((folder / 'alignment.request.json').read_text())
    assert q1['messages'] == c['request']['messages']
    assert q2['messages'][:len(q1['messages'])] == q1['messages']
    assert json.loads(q2['messages'][-2]['content'])['recorded_plan'] == p['subtasks']
    assert result['movement_steps'] == result['training_exports'] == 0


def test_failure_keeps_attempt_evidence_without_retry(tmp_path):
    calls = []
    def fail(*args):
        calls.append(1)
        raise TimeoutError('uncertain request')
    folder = tmp_path / 'attempt'
    result = annotate(fixture(), {'model': 'test'}, {}, ROOT / 'fixtures/media', folder, 'unused', fail)
    assert calls == [1]
    assert result['status'] == 'failed_or_uncertain'
    assert (folder / 'plan.started.json').exists()
    assert (folder / 'plan.request.json').exists()
    with pytest.raises(FileExistsError):
        write_new(folder / 'result.json', {})


def test_json_response_profile_preserves_tasks_and_raw_response(tmp_path):
    c = fixture()
    protocol = json.loads((ROOT / 'protocol.v1.json').read_text())
    p = plan()
    a = {'alignments': [row(1, [0]), row(2, [1])],
         'uncovered_action_chunk_ids': [], 'overall_note': 'test'}
    sent = []
    def call(endpoint, payload, config):
        sent.append(payload)
        assert 'tools' not in payload and 'tool_choice' not in payload
        assert payload['response_format']['type'] == 'json_schema'
        value = p if len(sent) == 1 else a
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(value)}}]}
    folder = tmp_path / 'json-attempt'
    result = annotate(c, {'model': 'any-model', 'response_mode': 'json_schema'}, protocol,
                      ROOT / 'fixtures/media', folder, 'unused', call)
    assert result['status'] == 'completed_audit'
    assert result['plan'] == p
    assert len(sent) == 2
    raw = json.loads((folder / 'plan.response.json').read_text())
    assert json.loads(raw['choices'][0]['message']['content']) == p
    assert 'tool_calls' not in raw['choices'][0]['message']
    with pytest.raises(ValueError):
        wire_request(c['request'], {'response_mode': 'unknown'})


@pytest.mark.parametrize('groups', [[[0, 1], [2]], [[0], [1, 2]], [[0, 1, 2]]])
def test_whole_chunk_partition_accepts_adjacent_merges(groups):
    validate_chunk_partition({'subtasks': [{'chunk_ids': ids} for ids in groups]}, ['a', 'b', 'c'])


@pytest.mark.parametrize('groups', [[[0], [0, 1, 2]], [[0], [2]], [[1], [0, 2]],
                                    [[0, 2], [1]], [[], [0, 1, 2]], [[False, 1, 2]]])
def test_whole_chunk_partition_rejects_overlap_gap_reorder_or_empty(groups):
    with pytest.raises(ValueError):
        validate_chunk_partition({'subtasks': [{'chunk_ids': ids} for ids in groups]}, ['a', 'b', 'c'])


@pytest.mark.parametrize('failure', ['partition', 'audit_reassignment'])
def test_whole_chunk_failure_preserves_evidence_without_retry(tmp_path, failure):
    c = json.loads((ROOT / 'fixtures/cases.whole-chunks.v1.json').read_text())[0]
    protocol = json.loads((ROOT / 'protocol.whole-chunks.v1.json').read_text())
    p = plan()
    for index, task in enumerate(p['subtasks']):
        task['chunk_ids'] = [index]
    if failure == 'partition':
        p['subtasks'][1]['chunk_ids'] = [0]
    a = {'alignments': [row(1, [1]), row(2, [0])],
         'uncovered_action_chunk_ids': [], 'overall_note': 'test'}
    calls = []

    def call(endpoint, payload, config):
        calls.append(payload)
        return response('update_plan', p) if len(calls) == 1 else response('record_chunk_alignment', a)

    folder = tmp_path / 'attempt'
    result = annotate(c, {'model': 'test'}, protocol, ROOT / 'fixtures/media', folder, 'unused', call)
    assert result['status'] == 'failed_or_uncertain'
    assert result['automatic_retry'] is False
    assert len(calls) == (1 if failure == 'partition' else 2)
    assert (folder / 'plan.response.json').exists()
    if failure == 'audit_reassignment':
        assert (folder / 'alignment.response.json').exists()
        assert json.loads((folder / 'frozen_plan.json').read_text()) == p
