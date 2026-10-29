"""One isolated LLM call per document to extract invoice/receipt fields as
strict JSON (ARCHITECTURE.md §10.3 step 3).

This never touches the main chat history: it builds its own single-message
conversation from just this document's row text, so a folder of dozens of
invoices never bloats the session's token budget or context window.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from deskai.core.errors import ProviderError
from deskai.core.events import Done, Error, TextDelta, ToolCallComplete, Usage
from deskai.core.messages import Message, Role, TextPart
from deskai.providers.base import ModelInfo, Provider

_SYSTEM_PROMPT = (
    "You extract structured fields from one invoice or receipt. Reply with "
    "strict JSON only — no markdown fences, no commentary — matching the "
    "given schema exactly. Copy every value as the raw string it appears as "
    "in the document; never convert, calculate, or reformat a number or "
    "date yourself. Use null for anything not present or not legible."
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class ExtractionError(Exception):
    """The isolated extraction call failed, or didn't return valid JSON."""


async def extract_fields(
    provider: Provider,
    model: ModelInfo,
    document_rows: list[str],
    schema: dict[str, object],
    *,
    on_usage: Callable[[Usage], None] = lambda usage: None,
) -> dict[str, object]:
    document_text = "\n".join(document_rows) if document_rows else "(no text extracted)"
    user_text = (
        "Document rows (top to bottom, left to right on each row):\n"
        f"{document_text}\n\n"
        "JSON schema to fill:\n"
        f"{json.dumps(schema)}"
    )
    history = [Message(Role.USER, [TextPart(user_text)])]

    text_buffer = ""
    try:
        async for event in provider.stream_chat(model, _SYSTEM_PROMPT, history, []):
            if isinstance(event, TextDelta):
                text_buffer += event.text
            elif isinstance(event, Usage):
                on_usage(event)
            elif isinstance(event, Error):
                raise ExtractionError(event.message)
            elif isinstance(event, Done | ToolCallComplete):
                pass
    except ProviderError as exc:
        raise ExtractionError(str(exc)) from exc

    return _parse_json_object(text_buffer)


def _parse_json_object(text: str) -> dict[str, object]:
    candidate = text.strip()
    match = _JSON_OBJECT_RE.search(candidate)
    if match:
        candidate = match.group(0)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model did not return valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExtractionError("model's JSON reply was not an object")
    return parsed
