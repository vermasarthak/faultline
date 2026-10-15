"""JSON decoding that rejects ambiguous duplicate object keys."""

import json


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
