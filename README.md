# Faultline

Find where an AI memory breaks. Prove whether a repair helps.

The first slice checks structured recall after a correction. It compares two deliberately simple synthetic memory fixtures. There are no model results or production claims yet.

Install the local checkout with Python 3.11 or newer:

```sh
python3 -m pip install .
faultline --help
```

Run directly from a checkout:

```sh
python3 -m faultline
python3 -m unittest discover -s tests -v
python3 -m faultline run --scenario examples/correction.json --adapter correction --attempts 3 --database evidence.db
python3 -m faultline compare --development-manifest examples/manifests/development.json --held-out-manifest examples/manifests/held-out.json --baseline stale --candidate correction --attempts 3
python3 -m faultline minimize --scenario examples/correction.json --adapter stale
python3 -m faultline sweep --adapter correction --seed 17 --scenarios 1000 --max-events 20
python3 -m faultline materialize --seed 17 --index 42 --max-events 20 > generated-case.json
python3 -m faultline benchmark --iterations 10000
python3 -m faultline run --scenario examples/correction.json --program examples/sqlite_memory.py
python3 -m faultline run --scenario examples/correction.json --program examples/sqlite_continuity.py --program-protocol continuity
FAULTLINE_TOKEN=replace-me python3 -m faultline run --scenario examples/correction.json --endpoint https://memory.example/evaluate --auth-token-env FAULTLINE_TOKEN
python3 -m faultline sweep --program examples/sqlite_continuity.py --program-protocol continuity --seed 17 --scenarios 20 --max-events 8
```

The local CLI emits JSON only. `run` executes a synthetic scenario with a declared budget and optional durable receipts. `compare` evaluates two adapters against a public synthetic held out manifest. `minimize` reduces a reproducible synthetic failure while preserving direct probe history. `show --database evidence.db --run-id ID` reads one receipt. `delete` removes one local receipt and `prune --database evidence.db --keep N` bounds retention. `show`, `delete`, `prune`, and `verify` require an existing database so a typo cannot create an empty store. `verify --database evidence.db` checks SQLite structure, stored receipt payloads, and indexed receipt metadata. Built in adapters are deliberately simple fixtures named `stale` and `correction`. `run --program`, `minimize --program`, `sweep --program`, and `compare --baseline-program` or `--candidate-program` can execute a trusted local memory program. `run --endpoint` executes one bounded snapshot against an HTTP adapter. `compare --baseline-endpoint` and `--candidate-endpoint` apply the same held out fail closed repair gate to remote adapters. Each remote side may read its bearer credential from its own named environment variable. Remote continuity, minimization, and sweep integration remain out of scope.

`benchmark` measures only the fixed synthetic harness workload. Its method and limitations are in [BENCHMARKS.md](BENCHMARKS.md).

The project builds a dependency free Python wheel named `faultline-memory`. The wheel installs the `faultline` console command and exposes the same module CLI. CI builds the wheel, installs it, and runs the command from outside the checkout so source tree imports cannot hide missing package files. Version `0.1.0` is an alpha interface, not a published registry release or stability promise.

The continuity protocol starts a new executable process for every observation and recall. Its SQLite example persists facts in a private trial directory, so the correction survives process restarts. The default snapshot protocol sends the whole event history to one process and cannot test that guarantee. Use `--program-protocol continuity` with `run`, `compare`, `minimize`, or `sweep` to check restart durability in individual runs, repair gates, minimized counterexamples, or generated histories. A comparison may mix a fixture and one continuity program, but at least one local program is required. Both examples are local reference implementations, not real AI memory systems. Continuity launches many processes and is not suitable for a harness throughput claim.

Use `compare --fail-on-regression` in a local check or CI job to emit the complete held out report and return a nonzero status when a candidate has a lower scored pass rate, fewer scored trials, or an inconclusive comparison on any held out scenario. Each comparison reports its verdict. This gate fails closed when an adapter outage removes the evidence needed to compare quality. It is an opt in guard, not a claim about real model quality.

`sweep` streams bounded write and delete histories from a declared seed and independent state oracle, then evaluates one adapter. Generator version two alternates probes for present and absent facts so an adapter that always returns unknown cannot pass by exploiting mostly absent probes. The report separates outcomes for present and absent expected states. Its generator version, seed, bounds, and failure fingerprints make the run reproducible. `materialize` replays the stream to turn a sweep index back into a version two scenario file for `minimize` without retaining earlier scenarios. Use `--generator-version 1` with both commands to replay older sweep indices and fingerprints. These are synthetic state checks, not representative model coverage.

The demo teaches an assistant that Mina lives in Rome, corrects it to Oslo, and asks for the current city. The stale fixture fails. The correction fixture passes. Both outputs include the scenario fingerprint and observed response.

Expected answers stay inside the evaluator. Adapters receive events and probes only. Unknown answers are reported separately from wrong answers. Each run starts with fresh memory.

## Scenario files

Scenario files are JSON objects with the exact keys `version`, `name`, `events`, `probe`, and `expected`. Version one supports string writes only. Version two makes each event operation explicit and permits `expected: null` for an intentional absence. A delete has a null value. Both versions reject unknown and duplicate fields. Direct `Event`, `Probe`, and `Scenario` construction enforces the same value contracts and requires an immutable event tuple. Scenario reads are capped at 256 KiB and manifest reads at 64 KiB, including when a file changes while it is being loaded. The loader exposes a raw content fingerprint while the runner records a canonical scenario fingerprint. Durable storage of both fingerprints arrives with persisted runs.

## Failure fixtures

Four first party JSON fixtures capture a distinct broken behavior: identity confusion, stale facts, invented facts, and deletion. Each fixture has a reference implementation that passes and a deliberately flawed implementation that fails. They are small synthetic checks, not claims about external memory systems.

## Adapter boundary

Each bounded run opens and closes a fresh adapter session. Adapters receive events and probes plus a deadline, and report a nonnegative integer microdollar cost. A malformed value or cost is an invalid response, while an exception raised by adapter code is a provider error. Faultline records timeout, operation budget, cost budget, invalid response, and provider error outcomes separately from wrong or unknown recall. Provider errors identify the safe lifecycle stage `open`, `observe`, `recall`, or `close` and the exception class without recording the exception message. In process Python adapters have cooperative deadlines. A local executable adapter runs in a new process group, which Faultline terminates on timeout or excess output. This is process control, not an operating system sandbox.

The executable protocol is for POSIX systems. It accepts one JSON object on standard input: `{"version":1,"events":[...],"probe":{"person":"Mina","field":"city"}}`. Each event has `person`, `field`, `value`, and `operation`. The expected answer is never sent. The program emits exactly one JSON object on standard output: `{"version":1,"value":"Oslo","cost_microusd":0}`. A null value means no recalled fact. Standard error is discarded. Requests are capped at 256 KiB and responses at 16 KiB. Each trial starts a new process, so the example SQLite program holds state only within one scenario. `--max-cost-microusd` checks reported cost after execution; it cannot prevent a program from spending money. Only run programs you trust. Do not point this interface at a paid provider without your own agreed budget and credential controls.

With `--program-protocol continuity`, Faultline passes a new temporary state directory as the program's only argument. Each process receives exactly one version two JSON request. Observe receives `{"version":2,"operation":"observe","event":{"person":"Mina","field":"city","value":"Oslo","operation":"write"}}` and must respond with `{"version":2,"cost_microusd":0}`. Recall receives `{"version":2,"operation":"recall","probe":{"person":"Mina","field":"city"}}` and must respond with `{"version":2,"value":"Oslo","cost_microusd":0}`. No process receives the full history or the expected answer. The directory is private to one attempt and removed on close. Each operation uses the same overall deadline and the executable input and output caps. The directory does not make the executable safe to run. See [examples/sqlite_continuity.py](examples/sqlite_continuity.py) for a complete adapter.

The HTTP snapshot protocol posts the same version one request body to `--endpoint` and expects the same version one response body as the local snapshot executable. Expected answers are not sent. Nonlocal endpoints must use HTTPS. Endpoint URLs containing credentials, queries, or fragments are rejected. `--auth-token-env NAME` reads a bearer token from the named environment variable; the token and endpoint are omitted from reports and receipts, while a SHA 256 endpoint identity is recorded. Redirects are rejected so authorization is not forwarded to another origin. Requests and responses use the same byte caps as executable adapters, and the overall run deadline becomes the network timeout. Reported cost is checked only after the response, so Faultline cannot prevent a remote provider from spending beyond the declared budget. Use a provider side spending cap before connecting a paid service.

## Durable evidence

CLI reports identify each fixture by name and each local executable by its SHA 256 file digest. `run` also includes that identity in a stored receipt. Faultline checks that the executable file has not changed by the end of an evaluation. A file digest does not capture imports, interpreter versions, environment, or a remote service used by the program.

`EvidenceStore` persists completed evidence in a local SQLite database. It accepts only declared schema version one fields with recognized statuses, valid fingerprints, nonempty run identifiers, timezone aware timestamps, and outcomes consistent with their expected and actual answers. When a receipt includes events and a probe, the store reconstructs the scenario and checks its fingerprint. This rejects undeclared payload fields but does not make scenario content safe to store. It uses WAL mode, full synchronous commits, a versioned schema, and an explicit immediate write transaction. New database files are owner only on POSIX systems; existing file permissions are unchanged. A successful `record` call commits one complete row. With `run --attempts N --database evidence.db`, all N attempts are committed in one transaction or none are. The report provides ordered `stored_run_ids` and retains `stored_run_id` as the final attempt for existing callers. A process interruption before commit leaves no partial batch. Integrity verification reads receipts incrementally rather than materializing the full table and validates the identity metadata used to order and retrieve them. This is local single machine storage. It is not encrypted, authenticated, or a multi tenant service.

Each store instance owns one SQLite connection and must remain in its creating thread. Separate store instances can write to the same database. SQLite serializes these writes with a five second busy timeout. Connection setup also retries WAL activation during concurrent first use. The concurrent writer test covers twelve independent local connections. This is not a load guarantee.

## Repeated trials

`repeat` records every recognized version one outcome, latency, and declared cost across identical repeated calls. It rejects a series with an unknown status or more than one scenario fingerprint. Only pass and fail outcomes form the reported quality rate. Unknown answers and execution errors remain visible but are not silently counted as quality failures. The Wilson interval describes sampling uncertainty only when trials are independent. Deterministic synthetic fixtures do not satisfy that assumption and their interval is not evidence of model quality. `compare_repairs` alternates one isolated baseline and candidate attempt per round and refuses to compare different scenario fingerprints.

## Held out repair checks

The development and held out manifests partition the synthetic fixtures by exact test content, excluding the scenario display name. A suite refuses overlap even if a case is renamed, and a manifest refuses repeated content. Receipt fingerprints still include the name. `compare_held_out` runs baseline and candidate over the held out manifest with the same declared budget configuration for each evaluation and emits results in manifest order. The held out fixtures are public and synthetic. Held out here means excluded from repair selection, not secret or representative of external model behavior.

## Failure minimization

`minimize_failure` uses bounded deterministic delta debugging and removes groups as well as individual events when the same failing status reproduces. It retains every event that directly targets the probe person and field, so corrections and deletion history for the queried fact remain intact. The result is one minimal under its tested removals, not proof of a globally smallest cause or root cause.

## Scope

This is a testing instrument for developers. It contains synthetic data only. It does not contain Sparkeefy conversations, prompts, schemas, or product logic.

Read [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md) before storing any nonpublic data locally.

Read [ARCHITECTURE.md](ARCHITECTURE.md), [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md), and [LIMITATIONS.md](LIMITATIONS.md) for the actual system boundaries and tradeoffs.

## Build checklist

1. DONE: reproducible correction fixture and structured evidence.
2. DONE: strict scenario files, content fingerprints, and validation.
3. DONE: adapter contract with timeouts, budgets, and session boundaries.
4. DONE: failure families for identity confusion, stale facts, invented facts, and deletion.
5. DONE: durable run storage and crash recovery.
6. DONE: repeated trials, uncertainty, and held out repair comparisons.
7. DONE: failure minimization that preserves direct scenario dependencies.
8. DONE: local CLI reports and focused evidence receipt viewer.
9. PARTIAL: independent local executable comparisons, process restart continuity checks, bounded remote snapshot runs, and remote repair comparisons are implemented. Independent real memory evaluations remain.
10. PARTIAL: CI, wheel build and installed CLI smoke checks, synthetic harness benchmarking, local privacy controls, and concurrent writer stress coverage are implemented. Registry publication and deployment remain.

Every completed issue needs tests, a reviewed diff, and a commit reference. Passing fixtures is not evidence that a real assistant works.
