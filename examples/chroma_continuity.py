#!/usr/bin/env python3
"""Optional Chroma persistence adapter for Faultline continuity evaluations."""

from hashlib import sha256
import json
from pathlib import Path
import sys

import chromadb
from chromadb.config import Settings


def key(person: str, field: str) -> str:
    return sha256(json.dumps([person, field], separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: chroma_continuity.py STATE_DIRECTORY")
    request = json.load(sys.stdin)
    if request.get("version") != 2:
        raise ValueError("unsupported protocol version")
    state = Path(sys.argv[1])
    client = chromadb.PersistentClient(
        path=str(state / "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_or_create_collection("faultline_memory", embedding_function=None)
    operation = request.get("operation")
    if operation == "observe":
        event = request["event"]
        identifier = key(event["person"], event["field"])
        if event["operation"] == "delete":
            collection.delete(ids=[identifier])
        else:
            collection.upsert(
                ids=[identifier],
                embeddings=[[0.0]],
                documents=[event["value"]],
            )
        response = {"version": 2, "cost_microusd": 0}
    elif operation == "recall":
        probe = request["probe"]
        result = collection.get(ids=[key(probe["person"], probe["field"])], include=["documents"])
        documents = result.get("documents") or []
        response = {
            "version": 2,
            "value": documents[0] if documents else None,
            "cost_microusd": 0,
        }
    else:
        raise ValueError("unsupported operation")
    print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
