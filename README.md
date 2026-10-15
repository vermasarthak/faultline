# Faultline

Find where an AI memory breaks. Prove whether a repair helps.

The first slice checks structured recall after a correction. It compares two deliberately simple synthetic memory fixtures. There are no model results or production claims yet.

Run with Python 3.11 or newer:

```sh
python3 -m faultline
python3 -m unittest discover -s tests -v
python3 -m faultline run --scenario examples/correction.json --adapter correction --attempts 3 --database evidence.db
python3 -m faultline benchmark --iterations 10000
```

The local CLI emits JSON only. `run` executes a synthetic scenario with a declared budget and optional durable receipt. `show --database evidence.db --run-id ID` reads one receipt. `delete` removes one local receipt and `prune --database evidence.db --keep N` bounds retention. Built in adapters are deliberately simple fixtures named `stale` and `correction`. There is no remote provider integration.

`benchmark` measures only the fixed synthetic harness workload. Its method and limitations are in [BENCHMARKS.md](BENCHMARKS.md).

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

Each store instance owns one SQLite connection and must remain in its creating thread. Separate store instances can write to the same database. SQLite serializes these writes with a five second busy timeout. The concurrent writer test covers twelve independent local connections. This is not a load guarantee.

## Repeated trials

`repeat` records every outcome, latency, and declared cost across identical repeated calls. Only pass and fail outcomes form the reported quality rate. Unknown answers and execution errors remain visible but are not silently counted as quality failures. The Wilson interval describes sampling uncertainty only when trials are independent. Deterministic synthetic fixtures do not satisfy that assumption and their interval is not evidence of model quality. `compare_repairs` refuses to compare baseline and candidate runs whose scenario fingerprints differ.

## Held out repair checks

The development and held out manifests partition the synthetic fixtures by canonical scenario fingerprint. A suite refuses overlap. `compare_held_out` runs baseline and candidate over the held out manifest with one shared budget and emits results in manifest order. The held out fixtures are public and synthetic. Held out here means excluded from repair selection, not secret or representative of external model behavior.

## Failure minimization

`minimize_failure` removes events in deterministic source order only when the same failing status reproduces. It retains every event that directly targets the probe person and field, so corrections and deletion history for the queried fact remain intact. It does not infer hidden semantic dependencies across unrelated fields. The reduction is an investigation aid, not a proof of root cause.

## Scope

This is a testing instrument for developers. It contains synthetic data only. It does not contain Sparkeefy conversations, prompts, schemas, or product logic.

Read [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md) before storing any nonpublic data locally.

## Build checklist

1. DONE: reproducible correction fixture and structured evidence.
2. DONE: strict scenario files, content fingerprints, and validation.
3. DONE: adapter contract with timeouts, budgets, and session boundaries.
4. DONE: failure families for identity confusion, stale facts, invented facts, and deletion.
5. DONE: durable run storage and crash recovery.
6. DONE: repeated trials, uncertainty, and held out repair comparisons.
7. DONE: failure minimization that preserves direct scenario dependencies.
8. DONE: local CLI reports and focused evidence receipt viewer.
9. Provider integration and independent memory system comparisons.
10. PARTIAL: CI, synthetic harness benchmarking, local privacy controls, and concurrent writer stress coverage are implemented. Deployment remains.

Every completed issue needs tests, a reviewed diff, and a commit reference. Passing fixtures is not evidence that a real assistant works.
