# Limitations

Faultline currently evaluates deterministic synthetic fixtures and can invoke a local executable memory program. Its snapshot example uses SQLite in memory. Its continuity example uses a trial scoped SQLite file and tests process restart survival. Neither example is an external model or production memory system. A passing fixture validates harness behavior, not a user outcome. Continuity does not test machine crashes or long term retention unless the selected adapter implements those semantics.

The optional Chroma adapter is an integration check against a real external persistence library. It uses exact fact identifiers and a local constant embedding, so it tests Chroma process restart persistence and deletion behavior rather than vector similarity, extraction, consolidation, or semantic retrieval. It is not evidence that a production AI memory system works.

Faultline deliberately has no HTTP adapter, API credential handling, hosted service, or model provider integration. Local executable programs are trusted code rather than sandboxed plugins, so a program supplied by a user can still access the network or local secrets with that user's permissions. Review programs before execution.

Repeated trial confidence intervals require independent trials. Deterministic fixtures do not meet that assumption. The benchmark measures a local Python harness only and is not a provider performance claim.

The local evidence database can be logically pruned or deleted, but not securely erased from filesystem history, SQLite journal files, backups, or snapshots.
