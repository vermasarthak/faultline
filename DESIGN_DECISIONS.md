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
