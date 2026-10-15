# Faultline

Find where an AI memory breaks. Prove whether a repair helps.

The first slice checks structured recall after a correction. It compares two deliberately simple synthetic memory fixtures. There are no model results or production claims yet.

Run with Python 3.11 or newer:

```sh
python3 -m faultline
python3 -m unittest discover -s tests -v
```

The demo teaches an assistant that Mina lives in Rome, corrects it to Oslo, and asks for the current city. The stale fixture fails. The correction fixture passes. Both outputs include the scenario fingerprint and observed response.

Expected answers stay inside the evaluator. Adapters receive events and probes only. Unknown answers are reported separately from wrong answers. Each run starts with fresh memory.

## Scenario files

Scenario files are JSON objects with the exact keys `version`, `name`, `events`, `probe`, and `expected`. Version one supports string writes only. Version two makes each event operation explicit and permits `expected: null` for an intentional absence. A delete has a null value. Both versions reject unknown fields. The loader exposes a raw content fingerprint while the runner records a canonical scenario fingerprint. Durable storage of both fingerprints arrives with persisted runs.

## Failure fixtures

Four first party JSON fixtures capture a distinct broken behavior: identity confusion, stale facts, invented facts, and deletion. Each fixture has a reference implementation that passes and a deliberately flawed implementation that fails. They are small synthetic checks, not claims about external memory systems.

## Adapter boundary

Each bounded run opens and closes a fresh adapter session. Adapters receive events, probes, a deadline, and a declared nonnegative microdollar cost. Faultline records timeout, operation budget, cost budget, invalid response, and provider error outcomes separately from wrong or unknown recall. A deadline is evidence that an adapter exceeded its time budget. It cannot terminate an arbitrary synchronous adapter process.

## Durable evidence

`EvidenceStore` persists completed evidence in a local SQLite database. It uses WAL mode, full synchronous commits, a versioned schema, and an explicit immediate write transaction. A successful `record` call has either committed one complete row or raised an error. A process interruption before commit leaves no partial row. This is local single machine storage. It is not encrypted, authenticated, or a multi tenant service.

## Repeated trials

`repeat` records every outcome, latency, and declared cost across identical repeated calls. Only pass and fail outcomes form the reported quality rate. Unknown answers and execution errors remain visible but are not silently counted as quality failures. The Wilson interval describes sampling uncertainty only when trials are independent. Deterministic synthetic fixtures do not satisfy that assumption and their interval is not evidence of model quality. `compare_repairs` refuses to compare baseline and candidate runs whose scenario fingerprints differ. Held out repair evaluation is still pending.

## Scope

This is a testing instrument for developers. It contains synthetic data only. It does not contain Sparkeefy conversations, prompts, schemas, or product logic.

## Build checklist

1. DONE: reproducible correction fixture and structured evidence.
2. DONE: strict scenario files, content fingerprints, and validation.
3. DONE: adapter contract with timeouts, budgets, and session boundaries.
4. DONE: failure families for identity confusion, stale facts, invented facts, and deletion.
5. DONE: durable run storage and crash recovery.
6. PARTIAL: repeated trials and uncertainty are implemented. Held out repair comparisons remain.
7. Failure minimization that preserves scenario dependencies.
8. CLI reports and a focused evidence viewer.
9. Provider integration and independent memory system comparisons.
10. Privacy controls, CI, stress testing, benchmarks, and deployment.

Every completed issue needs tests, a reviewed diff, and a commit reference. Passing fixtures is not evidence that a real assistant works.
