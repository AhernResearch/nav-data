"""Deterministic adjacent chunk grouping using measured trajectory distances."""
import math


def merge_short_chunks(chunks, short_distance_m=1.0):
    """Append short chunks to their predecessor; leading shorts join the next long chunk.

    Distances are measured along recorded frames, not inferred from stop/turn words.
    Chunk text and source IDs are never dropped or rewritten.
    """
    if not math.isfinite(short_distance_m) or short_distance_m <= 0:
        raise ValueError('positive finite short-distance threshold required')
    if not chunks or [c['chunk_id'] for c in chunks] != list(range(len(chunks))):
        raise ValueError('chunks must be nonempty and consecutively numbered')
    groups = []
    leading = []
    for chunk in chunks:
        distance = chunk['distance_m']
        if isinstance(distance, bool) or not math.isfinite(distance) or distance < 0:
            raise ValueError('chunk distance must be finite and nonnegative')
        if not isinstance(chunk['text'], str) or not chunk['text'].strip():
            raise ValueError('chunk text required')
        if distance < short_distance_m:
            if groups:
                groups[-1].append(chunk)
            else:
                leading.append(chunk)
        else:
            groups.append(leading + [chunk])
            leading = []
    if leading:
        groups.append(leading)
    return [{'chunk_ids': [c['chunk_id'] for c in group],
             'source_chunk_texts': [c['text'] for c in group],
             'instruction': ' '.join(c['text'].strip() for c in group),
             'distance_m': sum(c['distance_m'] for c in group)} for group in groups]
