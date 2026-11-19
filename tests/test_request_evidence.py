import copy
import hashlib

import pytest
from PIL import Image

from nav_data.release.evidence import compact_request, restore_request
from nav_data.tasks.region import build_request


def test_compact_evidence_restores_the_exact_request_without_mutating_model_input(
    tmp_path,
):
    public = {"images": {}}
    for i, view in enumerate(["front", "right", "back", "left"]):
        p = tmp_path / (view + ".png")
        Image.new("RGB", (16, 16), (i, 0, 0)).save(p)
        public["images"][view] = {
            "path": str(p),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
    request = build_request(public, {"model": "test"}, tmp_path)
    original = copy.deepcopy(request)
    record = compact_request(request, public)
    assert request == original and restore_request(record) == original
    assert record["request"]["messages"][1]["content"][2]["image_url"][
        "url"
    ].startswith("image-sha256:")
    (tmp_path / "front.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        restore_request(record)
