"""JSON decoding that rejects ambiguous duplicate object keys."""

import json
from pathlib import Path


def read_limited(path: str | Path, byte_limit: int, document_name: str) -> bytes:
    """Reads at most one byte beyond a document limit before rejecting it."""
    with Path(path).open("rb") as source:
        raw = source.read(byte_limit + 1)
    if len(raw) > byte_limit:
        raise ValueError(f"{document_name} exceeds {byte_limit} byte limit")
    return raw


def decode_object(raw: bytes, document_name: str) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise ValueError(f"{document_name} is not valid JSON") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result
