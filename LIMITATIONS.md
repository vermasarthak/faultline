# Limitations

Faultline currently evaluates deterministic synthetic fixtures and can invoke a local executable memory program. Its example program uses SQLite in memory and is not an external model or production memory system. A passing fixture validates harness behavior, not a user outcome.

There is no remote provider adapter, verified token accounting from a provider, remote tracing, production latency measurement, authorization, encryption, tenant isolation, deployment, or public hosted demo. Adding any of these requires an explicit budget, credentials, threat model, and operational design. Local executable programs are trusted code, not sandboxed plugins.

Repeated trial confidence intervals require independent trials. Deterministic fixtures do not meet that assumption. The benchmark measures a local Python harness only and is not a provider performance claim.

The local evidence database can be logically pruned or deleted, but not securely erased from filesystem history, SQLite journal files, backups, or snapshots.
