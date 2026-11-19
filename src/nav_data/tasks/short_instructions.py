"""Visible-region short-instruction collection, with one execution attempt per task."""

import time

from nav_data.planner_chunks.annotation import (
    http_call,
    parsed_call,
    tool_response,
    wire_request,
)
from nav_data.records import write
from nav_data.release.evidence import compact_request
from nav_data.tasks.region import REGION_TOOL, build_request


def propose_task(
    public, profile, folder, timeout, *, observation_folder=None, compact_evidence=False,
    response_mode="tool_call",
):
    request = build_request(
        public, profile, observation_folder or folder, target_mode="box"
    )
    response_config = {"response_mode": response_mode}
    request = wire_request(request, response_config)
    if compact_evidence:
        write(folder / "wire-request.json", compact_request(request, public))
    else:
        write(folder / "wire-request.json", request)
    started = time.perf_counter()
    response = http_call(profile["endpoint"], request, {"request_timeout_s": timeout})
    write(folder / "model-response.json", response)
    task, _ = parsed_call(tool_response(response, REGION_TOOL['function']['name'], response_config),
                          REGION_TOOL)
    return dict(task, model_wall_s=time.perf_counter() - started)
