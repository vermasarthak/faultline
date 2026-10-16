# Privacy

Faultline ships only synthetic scenarios. Do not add private conversations, customer records, proprietary prompts, API keys, or provider transcripts to this repository or its example files.

Local SQLite receipts may contain scenario names, expected values, observed values, error categories, timestamps, and fingerprints. Keep the database in a location whose filesystem permissions match the sensitivity of those values. Faultline sends no telemetry and has no remote storage in this release.

When `run` uses both `--attempts` and `--database`, every attempt becomes a receipt. Large repeat counts grow the database accordingly. Use the reported `stored_run_ids` to inspect or delete individual attempts, and apply retention deliberately.

Use `faultline delete --database evidence.db --run-id ID` to remove one receipt. Use `faultline prune --database evidence.db --keep N` to retain only the newest N receipts. These are logical SQLite deletions. They do not guarantee secure erasure from a filesystem, SQLite journal files, backups, or snapshots.
