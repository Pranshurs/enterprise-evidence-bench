"""Extraction of every string a request body carries to a model."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def all_strings(value: Any) -> Iterator[str]:
    """Every string value, recursively (dict keys excluded): messages, system prompts,
    tool definitions, tool results and metadata alike. Nothing in a JSON body escapes."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from all_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from all_strings(v)


def model_input_text(body: Any) -> str:
    return "\n".join(all_strings(body))
