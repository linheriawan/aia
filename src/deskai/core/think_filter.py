"""Streaming filter for inline reasoning markup (`<think>...</think>`) that
some local models (Qwen3, DeepSeek-R1-style) emit as part of their normal
text output, with no separate API field to distinguish it from the final
answer — unlike providers that expose a dedicated `reasoning_content` delta.

Pure text processing, no UI dependency, so callers (chat_window.py) just
decide how to render the two kinds of segment (e.g. dimmed vs normal color).
"""

from __future__ import annotations

_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"


class ThinkTagFilter:
    """Feed it streamed text chunks; it yields `(text, is_thinking)` pairs
    with the tags themselves removed. Tags split across chunk boundaries are
    held back in an internal buffer until they can be resolved."""

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False

    def feed(self, chunk: str) -> list[tuple[str, bool]]:
        self._buffer += chunk
        segments: list[tuple[str, bool]] = []
        while True:
            marker = _CLOSE_TAG if self._thinking else _OPEN_TAG
            index = self._buffer.find(marker)
            if index != -1:
                if index:
                    segments.append((self._buffer[:index], self._thinking))
                self._buffer = self._buffer[index + len(marker) :]
                self._thinking = not self._thinking
                continue
            hold_back = _partial_suffix_match(self._buffer, marker)
            emit_len = len(self._buffer) - hold_back
            if emit_len:
                segments.append((self._buffer[:emit_len], self._thinking))
            self._buffer = self._buffer[emit_len:]
            break
        return segments

    def flush(self) -> list[tuple[str, bool]]:
        """Call once the stream ends. Anything still buffered — e.g. a `<think>`
        the model never closed, or a tag-like fragment that was never
        completed — is emitted as-is rather than silently dropped."""
        if not self._buffer:
            return []
        segments = [(self._buffer, self._thinking)]
        self._buffer = ""
        return segments


def _partial_suffix_match(text: str, marker: str) -> int:
    """Length of the longest suffix of `text` that is also a prefix of
    `marker` — how many trailing characters to hold back in case the next
    chunk completes a tag that started at the end of this one."""
    max_len = min(len(text), len(marker) - 1)
    for length in range(max_len, 0, -1):
        if marker.startswith(text[-length:]):
            return length
    return 0
