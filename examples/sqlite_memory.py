#!/usr/bin/env python3
"""Independent local process adapter for the Faultline JSON protocol."""

import json
import sqlite3
import sys


def main() -> None:
    request = json.load(sys.stdin)
    if request["version"] != 1:
        raise ValueError("unsupported request version")
    with sqlite3.connect(":memory:") as database:
        database.execute(
            "CREATE TABLE facts (person TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, "
            "PRIMARY KEY (person, field))"
        )
        for event in request["events"]:
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
        probe = request["probe"]
        row = database.execute(
            "SELECT value FROM facts WHERE person = ? AND field = ?",
            (probe["person"], probe["field"]),
        ).fetchone()
    print(json.dumps({"version": 1, "value": row[0] if row else None, "cost_microusd": 0}))


if __name__ == "__main__":
    main()
