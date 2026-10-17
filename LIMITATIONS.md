# Limitations

Faultline currently evaluates deterministic synthetic fixtures and can invoke a local executable memory program or a remote HTTP snapshot adapter. Its snapshot example uses SQLite in memory. Its continuity example uses a trial scoped SQLite file and tests process restart survival. Neither example is an external model or production memory system. A passing fixture validates harness behavior, not a user outcome. Continuity does not test machine crashes, long term retention, or remote persistence unless the selected adapter implements those semantics.

The HTTP adapter provides bearer authentication and bounded snapshot runs and repair comparisons. There is no verified token accounting from a provider, remote tracing, production latency measurement, tenant isolation, remote continuity evaluation, deployment, or public hosted demo. Faultline cannot prove that an endpoint honors its declared cost or deletes submitted scenarios. Using a paid or nonpublic endpoint requires an explicit budget, credentials, data review, and provider side controls. Local executable programs are trusted code, not sandboxed plugins.

Repeated trial confidence intervals require independent trials. Deterministic fixtures do not meet that assumption. The benchmark measures a local Python harness only and is not a provider performance claim.

The local evidence database can be logically pruned or deleted, but not securely erased from filesystem history, SQLite journal files, backups, or snapshots.
