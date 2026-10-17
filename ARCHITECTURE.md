# Architecture

Faultline evaluates one synthetic memory scenario at a time. It is intentionally a local process, not a service.

## Data flow

1. `scenario.load` uses a bounded read for a strict versioned JSON document and retains a raw content fingerprint.
2. The evaluator keeps the expected answer. An adapter receives only events, a probe, and a deadline. The default local executable protocol sends one bounded request containing the events and probe. The opt in continuity protocol launches a new process for each event and for recall, sharing only a trial scoped state directory. Neither protocol sends the answer.
3. `execution.run_bounded` opens a fresh session, applies events in order, applies operation, cost, and deadline checks, then closes the session.
4. The evaluator emits structured evidence. Wrong recall, unknown recall, invalid output, provider error, and exhausted budgets remain distinct statuses.
5. `generate.generate_scenarios` derives bounded synthetic write and delete histories from a seed and independent state oracle. `trials.repeat` aggregates repeated executions. `heldout.compare_held_out` rejects overlapping development and held out scenario fingerprints before comparison. All three workflows can use the continuity protocol for local programs, while keeping each evaluation's state separate.
6. `store.EvidenceStore` commits one completed evidence object or an atomic batch of repeated attempt receipts to local SQLite.

## State and isolation

Scenario documents and evidence are immutable values once loaded. Every bounded evaluation opens a new adapter session. This avoids test order changing memory state. A continuity session creates a private temporary directory for one attempt and removes it on close. Its selected program owns any persistence inside that directory. The SQLite store owns durable receipts only. It has no queue, background worker, cache, or network client.

## Failure guarantees

SQLite receipts use an immediate transaction, WAL mode, and full synchronous commits. A successful record call has committed a complete row. A repeated run with durable storage commits all attempt receipts as one batch. An interruption before commit leaves no partial row or partial batch. Separate store instances serialize concurrent writes through SQLite. Concurrent connection setup retries WAL activation for up to five seconds. A shared store instance is not thread safe.

In process adapter deadlines are cooperative. Faultline can detect that a synchronous adapter returned after its deadline, but cannot kill arbitrary code in its own process. Local executable adapters run in their own process group; timeout and oversized output terminate that group. This does not isolate the executable from files or the network. Faultline never claims exactly once provider execution or exactly once model inference.

## Trust boundary

Scenarios are untrusted local inputs. Strict scenario parsing rejects unknown schema fields. Adapter output is checked before evidence is recorded, and durable receipts must have outcomes consistent with their expected and actual answers. Expected answers never cross the adapter boundary. Provider exception messages are not included in evidence. A selected executable is trusted code with the user's file and network permissions, not an isolated plugin. Faultline itself has no outbound network adapter or inbound service.
