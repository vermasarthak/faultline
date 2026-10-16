# Architecture

Faultline evaluates one synthetic memory scenario at a time. It is intentionally a local process, not a service.

## Data flow

1. `scenario.load` uses a bounded read for a strict versioned JSON document and retains a raw content fingerprint.
2. The evaluator keeps the expected answer. An adapter receives only events, a probe, and a deadline.
3. `execution.run_bounded` opens a fresh session, applies events in order, applies operation, cost, and deadline checks, then closes the session.
4. The evaluator emits structured evidence. Wrong recall, unknown recall, invalid output, provider error, and exhausted budgets remain distinct statuses.
5. `generate.generate_scenarios` derives bounded synthetic write and delete histories from a seed and independent state oracle. `trials.repeat` aggregates repeated executions. `heldout.compare_held_out` rejects overlapping development and held out scenario fingerprints before comparison.
6. `store.EvidenceStore` can commit one completed evidence object as a local SQLite receipt.

## State and isolation

Scenario documents and evidence are immutable values once loaded. Every bounded evaluation opens a new adapter session. This avoids test order changing memory state. The SQLite store owns durable receipts only. It has no queue, background worker, remote transport, or cache.

## Failure guarantees

SQLite receipts use an immediate transaction, WAL mode, and full synchronous commits. A successful record call has committed a complete row. An interruption before commit leaves no partial row. Separate store instances serialize concurrent writes through SQLite. Concurrent connection setup retries WAL activation for up to five seconds. A shared store instance is not thread safe.

Adapter deadlines are cooperative. Faultline can detect that a synchronous adapter returned after its deadline, but cannot kill arbitrary adapter code. It never claims exactly once provider execution or exactly once model inference.

## Trust boundary

Scenarios and adapters are local untrusted inputs. Strict scenario parsing rejects unknown schema fields. Adapter output is checked before evidence is recorded, and durable receipts must have outcomes consistent with their expected and actual answers. Expected answers never cross the adapter boundary. Provider exception messages are not included in evidence. The current release includes no credential, network, authentication, tenant, or tool execution boundary.
