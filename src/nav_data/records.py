"""Durable create-only JSON records and content hashes for released production evidence.

Completed attempts are never overwritten: ``write`` fails if the path exists, so a
retry cannot silently replace evidence that a receipt already referenced.
"""

import hashlib
import json
import os
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def write_text_atomic(path, text):
    """Replace a progress file only after its complete content is on disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text)
    temporary.replace(path)


class RejectedCandidate(ValueError):
    """Known geometry rejection before any actual movement."""
