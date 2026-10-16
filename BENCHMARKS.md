# Synthetic harness benchmark

Run:

```sh
python3 -m faultline benchmark --iterations 10000
```

The command repeatedly runs the built in stale and correction memory fixtures against one fixed correction scenario in a single Python process. It reports the scenario fingerprint, Python implementation and version, elapsed wall time, runs per second, outcome counts, and the fixed number of event and probe operations per run.

This is a reproducible workload, not a reproducible machine result. CPU model, Python version, thermal state, and competing processes affect elapsed time. It does not include model inference, provider networking, storage, concurrency, or token costs. The values must not be used as a claim about external assistant performance.

## Large synthetic sweep

On 17 October 2026, an Apple M1 running Python 3.14.7 completed this local workload with 10,000 passes, 27.87 seconds wall time, and 25,018,368 bytes maximum resident size as reported by `/usr/bin/time -l`:

```sh
/usr/bin/time -l python3 -m faultline sweep --adapter correction --seed 29 --scenarios 10000 --max-events 1000 --max-operations 1001 --max-failures 1
```

The CLI streams each generated scenario instead of retaining the full suite. This measurement covers synthetic generation and local evaluation only. It is one observed run, not a service level target or an external memory benchmark.
