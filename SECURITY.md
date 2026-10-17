# Security

Faultline is a local developer tool. It has no HTTP adapter, API credential handling, inbound service, user authorization, or tenant isolation. Do not treat its evidence database as a multi tenant system.

Scenario parsing uses strict schemas and rejects unknown fields. Held out manifest entries must resolve inside their fixture directory, including when a local symlink is present. Adapter output is validated before it becomes evidence. Adapter exceptions are classified by type without recording their message, which reduces accidental provider error leakage.

`--program`, `--baseline-program`, and `--candidate-program` execute the selected local file with the user's permissions. Use only a trusted executable. The process receives scenario events and probes, may read local files or use the network, and is not sandboxed. The expected answer stays in Faultline. Faultline discards standard error, caps standard output, and kills the process group on timeout, but these controls do not stop a malicious program from accessing data or spending money. Keep secrets outside scenario files and do not attach an unapproved paid provider.

The continuity protocol passes a temporary directory path to each process in a trial. Faultline removes it after the attempt, but that is logical cleanup, not secure erasure. The executable can copy data elsewhere, and an abnormal host shutdown can leave a temporary directory behind. Do not put private user data into synthetic scenarios.

Each `EvidenceStore` instance owns one SQLite connection and must stay in its creating thread. Use a separate store instance for each concurrent writer. SQLite serializes write transactions with a five second busy timeout. A sustained write load beyond that timeout can return a lock error and needs caller level retry or a different storage design.

When creating a new evidence database on POSIX, Faultline requests mode 0600 before SQLite opens it. Existing database permissions are left alone. This is a local default, not a defense against a hostile user with access to the parent directory, backups, or the same operating system account.

Run `faultline verify --database evidence.db` before relying on old local evidence. It checks SQLite structural integrity and validates every stored evidence payload. It cannot detect filesystem rollback, malicious modification that remains structurally valid, or missing backup data.

Any hosted Faultline service or multi tenant integration needs a separate threat model covering tenant isolation, inbound authorization, auditability, abuse limits, and input and output policies.
