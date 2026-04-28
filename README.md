# damyan-bench

Local benchmark suite for OpenAI-compatible model endpoints, focused on coding, tool-use, boundary behavior, and operator-style tasks.

“OpenAI-compatible” means API-shape compatible endpoints, including local servers such as llama.cpp-style runtimes; the suite is not tied to a specific hosted provider.

## What It Tests

- agentic coding task execution
- structured tool-call behavior
- boundary and safety edge cases
- local OpenAI-compatible server behavior
- regression comparison across models and sampling configs

## Layout

- `benchmarks/agentic-coding/`: current full coding/operator benchmark profile
- `benchmarks/minimax-m27-authoring/`: original authoring-only suite
- `benchmarks/minimax-m27-structured*/`: legacy structured output probes
- `benchmarks/minimax-m27-toolcall-high*/`: legacy tool-call stress probes

## Quick start

```bash
cd benchmarks/agentic-coding
python3 run_suite.py --base http://127.0.0.1:1234 --model YOUR_MODEL_NAME
```

Run a single group:

```bash
python3 run_suite.py --base http://127.0.0.1:1234 --model YOUR_MODEL_NAME --group "Code Debugging"
```

Resume from saved raw responses:

```bash
python3 run_suite.py --base http://127.0.0.1:1234 --model YOUR_MODEL_NAME --resume
```

Overnight run:

```bash
DAMYAN_BENCH_BASE=http://127.0.0.1:1234 \
DAMYAN_BENCH_MODEL=YOUR_MODEL_NAME \
./run_overnight.sh
```

## Notes

- The runner expects an OpenAI-compatible `/v1/chat/completions` endpoint.
- Reports, logs, raw responses, generated artifacts, and validation scratch files stay inside each benchmark directory.
- `run_forensics.sh` is Linux/ROCm-oriented and is useful when diagnosing long-running llama-server router/model failures.
- The public sample report is synthetic and intentionally contains no raw model output, real endpoint, provider token, absolute path, private prompt, or timestamped work trace.

## Sample Report

See [`benchmarks/agentic-coding/reports/sample-synthetic.md`](benchmarks/agentic-coding/reports/sample-synthetic.md) for the public report shape.

---

Maintained by [Damyan Deshev](https://github.com/damyan-deshev) - local-first software, deterministic data paths, retrieval, evaluation, and practical product systems.
