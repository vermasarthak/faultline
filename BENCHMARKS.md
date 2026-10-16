# Synthetic harness benchmark

Run:

```sh
python3 -m faultline benchmark --iterations 10000
```

The command repeatedly runs the built in stale and correction memory fixtures against one fixed correction scenario in a single Python process. It reports the scenario fingerprint, Python implementation and version, elapsed wall time, runs per second, outcome counts, and the fixed number of event and probe operations per run.

This is a reproducible workload, not a reproducible machine result. CPU model, Python version, thermal state, and competing processes affect elapsed time. It does not include model inference, provider networking, storage, concurrency, or token costs. The values must not be used as a claim about external assistant performance.
