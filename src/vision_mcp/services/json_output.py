"""JSON extraction / limited repair helpers.

CLI providers sometimes wrap JSON in prose or fail to escape a field. We do a
*bounded* extraction: find the outermost JSON object/array, attempt a small
list of repairs (``` fences, trailing commas), and give up — never an infinite
retry loop.
"""

from __future__ import annotations

import json
import re


class JsonExtractionError(ValueError):
    pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _find_json_span(text: str) -> tuple[int, int]:
    """Return (start, end) of the outermost balanced JSON value."""
    start = -1
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0 and start >= 0:
                return start, i + 1
    if start < 0:
        raise JsonExtractionError("no JSON value found")
    raise JsonExtractionError("unbalanced JSON value")


def _try_parse(text: str):
    text = text.strip()
    if not text:
        raise JsonExtractionError("empty JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Repair: strip trailing commas inside objects/arrays.
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    raise JsonExtractionError("malformed JSON after bounded repair")


def extract_json(text: str):
    """Extract a JSON value from ``text``, allowing surrounding prose."""
    if not text:
        raise JsonExtractionError("empty output")
    # 1. Try a fenced block first.
    m = _FENCE.search(text)
    if m:
        try:
            return _try_parse(m.group(1))
        except JsonExtractionError:
            pass
    # 2. Try the whole string.
    try:
        return _try_parse(text)
    except JsonExtractionError:
        pass
    # 3. Try the outermost balanced JSON value.
    try:
        start, end = _find_json_span(text)
        return _try_parse(text[start:end])
    except JsonExtractionError:
        raise JsonExtractionError("could not extract JSON from provider output")