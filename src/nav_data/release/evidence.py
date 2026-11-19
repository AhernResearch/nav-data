"""Lossless model-request evidence referencing the already retained current PNGs."""

import base64
import copy
import hashlib
import json
from pathlib import Path


def request_digest(request):
    return hashlib.sha256(
        json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def compact_request(request, public):
    result = copy.deepcopy(request)
    images = {}
    for item in public["images"].values():
        path = Path(item["path"])
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != item["sha256"]:
            raise ValueError("current image changed before request evidence")
        images["image-sha256:" + sha] = {
            "path": str(path),
            "sha256": sha,
            "data_url": "data:image/png;base64," + base64.b64encode(data).decode(),
        }
    for message in result["messages"]:
        if isinstance(message["content"], list):
            for part in message["content"]:
                if part.get("type") == "image_url":
                    match = next(
                        (
                            key
                            for key, value in images.items()
                            if value["data_url"] == part["image_url"]["url"]
                        ),
                        None,
                    )
                    if match is None:
                        raise ValueError("request contains an unrecorded image")
                    part["image_url"]["url"] = match
    return {
        "schema_version": 1,
        "encoding": "request_with_retained_png_references",
        "request": result,
        "images": {
            k: {f: v for f, v in item.items() if f != "data_url"}
            for k, item in images.items()
        },
        "canonical_request_sha256": request_digest(request),
    }


def restore_request(evidence):
    result = copy.deepcopy(evidence["request"])
    for message in result["messages"]:
        if isinstance(message["content"], list):
            for part in message["content"]:
                if part.get("type") == "image_url":
                    item = evidence["images"][part["image_url"]["url"]]
                    data = Path(item["path"]).read_bytes()
                    if hashlib.sha256(data).hexdigest() != item["sha256"]:
                        raise ValueError("retained model image digest mismatch")
                    part["image_url"]["url"] = (
                        "data:image/png;base64," + base64.b64encode(data).decode()
                    )
    if request_digest(result) != evidence["canonical_request_sha256"]:
        raise ValueError("reconstructed model request differs from frozen evidence")
    return result
