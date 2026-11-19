"""Observation-only public model boundary; no history or private trajectory fields."""
import base64
from pathlib import Path

from nav_data.planner_chunks.annotation import http_call, parsed_call
from nav_data.records import digest, write

PROMPT = '''Write one local execution instruction from the assigned task and current camera observations. The four views are simultaneous, not history. The robot may already be partway through the task. Describe the remaining action from its current position and orientation; do not repeat a visibly completed turn or crossing. Preserve required route constraints, counts and the intended endpoint, especially distinctions such as outside/inside a doorway or the last stair. Ground relative directions and landmarks in the images. If the destination or remaining action cannot be identified, set needs_review=true instead of inventing it. A local policy learns to STOP and return control to its Planner: ordinary stop/wait words need not be repeated, but do not change the stopping location or remaining task scope. Return a concise self-contained instruction with no reasoning inside it, using write_execution_instruction. No movement is executed by this tool.'''
TOOL = {'type': 'function', 'function': {'name': 'write_execution_instruction',
        'description': 'Record a current-observation execution instruction, without movement.',
        'parameters': {'type': 'object', 'additionalProperties': False,
            'properties': {'instruction': {'type': 'string', 'minLength': 1},
                           'needs_review': {'type': 'boolean'}, 'reason': {'type': 'string'}},
            'required': ['instruction', 'needs_review', 'reason']}}}


def build_request(public, profile, folder):
    if set(public) != {'instruction', 'images'} or set(public['images']) != {'front', 'right', 'back', 'left'}:
        raise ValueError('only local task and four current images are model-visible')
    if not isinstance(public['instruction'], str) or not public['instruction'].strip():
        raise ValueError('nonempty task instruction required')
    content = [{'type': 'text', 'text': 'Assigned local task:\n' + public['instruction']}]
    for label in ('front', 'right', 'back', 'left'):
        item = public['images'][label]
        if set(item) != {'path', 'sha256'}:
            raise ValueError('unexpected image metadata')
        path = Path(item['path']).resolve()
        if path.parent != Path(folder).resolve() or path.name != label + '.png' or digest(path) != item['sha256']:
            raise ValueError('current image identity mismatch')
        content += [{'type': 'text', 'text': 'Current view: ' + label},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()}}]
    return {'model': profile['model'], 'messages': [{'role': 'system', 'content': PROMPT},
             {'role': 'user', 'content': content}], 'tools': [TOOL],
            'tool_choice': {'type': 'function', 'function': {'name': 'write_execution_instruction'}},
            'parallel_tool_calls': False, 'temperature': 0, 'max_tokens': 1024,
            'chat_template_kwargs': {'enable_thinking': False}}


def annotate(public, profile, folder, timeout, *, observation_folder=None):
    request = build_request(public, profile, observation_folder or folder)
    write(folder / 'wire-request.json', request)
    response = http_call(profile['endpoint'], request, {'request_timeout_s': timeout})
    write(folder / 'model-response.json', response)
    value, _ = parsed_call(response, TOOL)
    if not value['instruction'].strip():
        raise ValueError('empty execution instruction')
    write(folder / 'instruction.json', value)
    return value
