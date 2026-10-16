#!/usr/bin/env python3
"""A durable local reference for the continuity protocol, not a model benchmark."""

import json
from pathlib import Path
import sqlite3
import sys


def main() -> None:
    state = Path(sys.argv[1])
    request = json.load(sys.stdin)
    if request["version"] != 2:
        raise ValueError("unsupported request version")
    with sqlite3.connect(state / "memory.sqlite3") as database:
        database.execute(
            "CREATE TABLE IF NOT EXISTS facts (person TEXT NOT NULL, field TEXT NOT NULL, "
            "value TEXT NOT NULL, PRIMARY KEY (person, field))"
        )
        if request["operation"] == "observe":
            event = request["event"]
            if event["operation"] == "delete":
                database.execute(
                    "DELETE FROM facts WHERE person = ? AND field = ?",
                    (event["person"], event["field"]),
                )
            else:
                database.execute(
                    "INSERT INTO facts (person, field, value) VALUES (?, ?, ?) "
                    "ON CONFLICT (person, field) DO UPDATE SET value = excluded.value",
                    (event["person"], event["field"], event["value"]),
                )
            response = {"version": 2, "cost_microusd": 0}
        elif request["operation"] == "recall":
            probe = request["probe"]
            row = database.execute(
                "SELECT value FROM facts WHERE person = ? AND field = ?",
                (probe["person"], probe["field"]),
            ).fetchone()
            response = {"version": 2, "value": row[0] if row else None, "cost_microusd": 0}
        else:
            raise ValueError("unsupported operation")
    print(json.dumps(response))


if __name__ == "__main__":
    main()
