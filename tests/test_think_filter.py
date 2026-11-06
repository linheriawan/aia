from deskai.core.think_filter import ThinkTagFilter


def _run(chunks: list[str]) -> list[tuple[str, bool]]:
    filt = ThinkTagFilter()
    segments: list[tuple[str, bool]] = []
    for chunk in chunks:
        segments.extend(filt.feed(chunk))
    segments.extend(filt.flush())
    return segments


def test_no_think_tags_passes_through_unchanged() -> None:
    assert _run(["hello ", "world"]) == [("hello ", False), ("world", False)]


def test_think_block_in_one_chunk() -> None:
    assert _run(["<think>reasoning</think>answer"]) == [
        ("reasoning", True),
        ("answer", False),
    ]


def test_think_tags_split_across_chunk_boundaries() -> None:
    chunks = ["<thi", "nk>", "reaso", "ning", "</th", "ink>", "ans", "wer"]
    assert _run(chunks) == [
        ("reaso", True),
        ("ning", True),
        ("ans", False),
        ("wer", False),
    ]


def test_unclosed_think_block_flushes_as_thinking() -> None:
    assert _run(["before<think>never closes"]) == [
        ("before", False),
        ("never closes", True),
    ]


def test_text_ending_in_partial_tag_prefix_is_held_until_resolved() -> None:
    filt = ThinkTagFilter()
    assert filt.feed("hello <th") == [("hello ", False)]
    assert filt.feed("ink>world") == [("world", True)]


def test_angle_bracket_not_at_chunk_end_is_not_held_back() -> None:
    # Only a `<` (etc.) right at the END of a chunk is ambiguous — one that's
    # already followed by more text in the same chunk can't be a split tag.
    filt = ThinkTagFilter()
    assert filt.feed("a < b") == [("a < b", False)]
    assert filt.flush() == []


def test_lone_trailing_angle_bracket_is_held_then_flushed() -> None:
    filt = ThinkTagFilter()
    assert filt.feed("a <") == [("a ", False)]
    assert filt.flush() == [("<", False)]


def test_multiple_think_blocks() -> None:
    chunks = ["<think>a</think>mid<think>b</think>end"]
    assert _run(chunks) == [
        ("a", True),
        ("mid", False),
        ("b", True),
        ("end", False),
    ]
