# Design decisions

## Keep expected answers inside the evaluator

Adapters receive observations and probes but not the expected answer. This avoids an evaluation that can be passed by copying an answer supplied in its input. It does not protect against an adapter already trained on public fixtures.

## Use small versioned JSON scenarios

Version one supports simple writes. Version two adds explicit write and delete operations plus expected absence. Unknown fields are rejected so scenario meaning cannot change silently. Larger natural language scenarios remain out of scope until their semantics can be made equally inspectable.

## Use SQLite for local durable receipts

SQLite supplies crash recovery and serialized writes without adding a service. It fits a local developer tool. It is not a tenant store, encrypted vault, or distributed database.

## Separate quality from execution outcomes

Pass and fail contribute to a quality rate. Unknown responses, timeouts, provider errors, invalid responses, and budget outcomes remain explicit. Collapsing them into a pass rate would make a broken integration appear like a weak memory system.

## Retain direct probe history during minimization

The reducer may remove an event only if the same failure reproduces. It always preserves events for the queried person and field. This keeps correction and deletion history visible, but cannot infer every causal relationship across unrelated fields.

## Test continuity separately from snapshot recall

The snapshot protocol is cheap and useful for checking final state semantics, but a program can pass by rebuilding its memory from the complete history supplied with every query. Continuity instead runs one process per observation and recall and supplies only a trial scoped directory for shared state. This exposes lost state across process restarts. A persistent remote service might use a separate store outside that directory, so the adapter must isolate its own trial data and cleanup. The extra process launches make continuity slower; use it for durability checks rather than harness throughput measurements.

## Balance generated recall and absence probes

The first generator sampled a probe uniformly from twelve person and field pairs. Sparse histories therefore favored expected absence, allowing an adapter that always returned unknown to pass many cases. Generator version two alternates between present and absent expected states, favoring corrected or deleted keys when available. If an event history cannot provide the required state, it changes its final event and recomputes the oracle. This is a deliberate coverage policy rather than a model quality estimate. The original generator remains selectable by version so old sweep fingerprints can still be reproduced. Revisit this policy when adding richer memory semantics or representative external evaluation data.
