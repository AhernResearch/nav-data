import pytest

from nav_data.planner_chunks.merging import merge_short_chunks


def chunks(lengths):
    return [{'chunk_id': i, 'text': f'action {i}', 'distance_m': d} for i, d in enumerate(lengths)]


@pytest.mark.parametrize('lengths,expected', [
    ([2, .2, 0, 3], [[0, 1, 2], [3]]),
    ([0, .2, 2, .1, 4], [[0, 1, 2, 3], [4]]),
    ([0, .1, 0], [[0, 1, 2]]),
    ([1, 1], [[0], [1]]),
])
def test_merge_preserves_order_and_whole_chunks(lengths, expected):
    result = merge_short_chunks(chunks(lengths))
    assert [r['chunk_ids'] for r in result] == expected
    assert [t for r in result for t in r['source_chunk_texts']] == [f'action {i}' for i in range(len(lengths))]


@pytest.mark.parametrize('bad', [-1, float('nan'), float('inf'), True])
def test_invalid_measurements_rejected(bad):
    with pytest.raises(ValueError):
        merge_short_chunks(chunks([bad]))


def test_stop_word_does_not_imply_zero_motion():
    data = chunks([2, 4])
    data[1]['text'] = 'go to the doorway and stop there'
    assert len(merge_short_chunks(data)) == 2
