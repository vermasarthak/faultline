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

Scenario files are JSON objects with the exact keys `version`, `name`, `events`, `probe`, and `expected`. Version one accepts only string values and rejects unknown fields. The loader exposes a raw content fingerprint while the runner records a canonical scenario fingerprint. Durable storage of both fingerprints arrives with persisted runs.

## Scope

This is a testing instrument for developers. It contains synthetic data only. It does not contain Sparkeefy conversations, prompts, schemas, or product logic.

## Build checklist

1. DONE: reproducible correction fixture and structured evidence.
2. NEXT: strict scenario files, content fingerprints, and validation.
3. Adapter contract with timeouts, budgets, and session boundaries.
4. Failure families for identity confusion, stale facts, invented facts, and deletion.
5. Durable run storage and crash recovery.
6. Repeated trials with uncertainty and held out repair comparisons.
7. Failure minimization that preserves scenario dependencies.
8. CLI reports and a focused evidence viewer.
9. Provider integration and independent memory system comparisons.
10. Privacy controls, CI, stress testing, benchmarks, and deployment.

Every completed issue needs tests, a reviewed diff, and a commit reference. Passing fixtures is not evidence that a real assistant works.
