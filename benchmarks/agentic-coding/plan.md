# Damyan Bench Agentic Coding Plan

- Default model profile: `MiniMax-M2.7-UD-IQ3_S`
- Intended production coding mode: `enable_thinking=true`
- Proposed baseline sampling for coding evals: `temperature=0.2`, `top_p=0.95`, `top_k=40`
- Source of truth for cases: `benchmarks/agentic-coding/suite.json`

## Why keep a max token budget at all

Removing `max_tokens` entirely is a bad harness default for reasoning-on coding tests because:
- it hides looping/failure-to-terminate pathologies
- it makes overnight runs operationally risky
- it turns token runaway into a hardware/runtime problem instead of a measured model behavior

The better approach is:
- use **large but bounded** budgets
- size them per task complexity
- treat `finish_reason = length` as a calibration signal, not as automatic model failure


## Added real-world failure surfaces

The suite now explicitly covers:
- local edit discipline
- one-retry repair realism
- dependency / import hallucination discipline
- tool unavailable honesty
- history contamination
- instruction hierarchy conflicts

These are closer to VS Code plugin reality than whole-file generation alone.

## Budget policy

All cases currently use a flat `max_tokens = 12000`.

Rationale:
- `enable_thinking=true` means the budget must cover both hidden reasoning and final visible output
- repeated truncation is currently a more important failure mode than cost or latency
- the remaining reason to keep a budget at all is loop containment rather than fine-grained cost control
- the serving context is large enough that a 12k completion ceiling is operationally acceptable for this suite

## Retry protocol

### R1 — Validator retry
If a code task fails build / runtime / validator:
- retry once
- include the original prompt
- include the model's previous answer
- include only the relevant validator error output

Measure:
- `pass@1`
- `pass after 1 validator retry`

### R2 — Envelope retry
If the model returns the wrong envelope:
- prose instead of pure code
- markdown fences when raw code is required
- tool call when code-only was expected
- code when tool-only was expected

Retry once with a hard contract reminder, e.g.:
- `Wrong envelope. Return only code.`
- `Wrong mode. Do not use tools.`
- `Return only the native tool call. No prose.`

Measure:
- `pass after 1 envelope retry`

## Validation policy

### Authoring / Debugging
- Save raw model response
- Extract code
- Save generated artifact
- Run syntax/build checks
- Run minimal behavioral checks
- Perform short read-through after automated validation

### Local Edit Discipline
For edit-oriented tasks also measure:
- `diff-size ratio`
- `unchanged-region corruption rate`

### Dependency Discipline
For relevant tasks also measure:
- `invented dependency/import rate`
- `silent dependency smuggling`

### Tool / Boundary
- Validate mode selection
- Validate tool choice and args when tool mode is expected
- Validate **no tool call** when code-only mode is expected
- Validate honest failure when a requested tool is unavailable
- Treat non-empty prose around tool calls as at least a soft failure
- Track `fake execution claim rate`
- Track `tool hallucination rate`

## Execution order

1. Code Authoring
2. Code Debugging / Edit Discipline
3. Tool Discipline / Agent Boundary
4. Retry passes for failures

## Runner

Primary runner:
- `python3 benchmarks/agentic-coding/run_suite.py`

Useful invocations:
- Authoring only:
  - `python3 benchmarks/agentic-coding/run_suite.py --group "Code Authoring"`
- Debugging only:
  - `python3 benchmarks/agentic-coding/run_suite.py --group "Code Debugging"`
- Tool/boundary only:
  - `python3 benchmarks/agentic-coding/run_suite.py --group "Tool Discipline / Agent Boundary"`
- Single case:
  - `python3 benchmarks/agentic-coding/run_suite.py --case T7`
- Resume with existing raw artifacts:
  - `python3 benchmarks/agentic-coding/run_suite.py --resume`

Artifacts:
- Raw model responses: `benchmarks/agentic-coding/raw/`
- Generated code artifacts: `benchmarks/agentic-coding/responses/`
- Validation harness files: `benchmarks/agentic-coding/artifacts/`
- Phase reports: `benchmarks/agentic-coding/reports/`

## Reporting

Per phase:
- exact prompt/task summary
- expected behavior
- received response summary
- validator output
- status
- retry outcome if applicable

Statuses:
- `OK`
- `PARTIAL`
- `НЕ-ОК`

For tool cases also allow:
- `SOFT FAIL` for correct tool/args with prose leakage
- `HARD FAIL` for wrong mode / wrong tool / wrong args / missing tool call / fake execution claim
