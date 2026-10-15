# Limitations

Faultline currently evaluates deterministic synthetic fixtures and two built in memory examples. A passing fixture validates harness behavior, not an external model, production memory system, or user outcome.

There is no provider adapter, token accounting from a provider, remote tracing, real latency measurement, authorization, encryption, tenant isolation, deployment, or public hosted demo. Adding any of these requires an explicit budget, credentials, threat model, and operational design.

Repeated trial confidence intervals require independent trials. Deterministic fixtures do not meet that assumption. The benchmark measures a local Python harness only and is not a provider performance claim.

The local evidence database can be logically pruned or deleted, but not securely erased from filesystem history, SQLite journal files, backups, or snapshots.
