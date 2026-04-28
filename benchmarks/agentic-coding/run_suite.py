#!/usr/bin/env python3
import argparse
import difflib
from datetime import datetime
import json
import re
import shutil
import subprocess
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any
from urllib import request, error as urlerror


DEFAULT_BASE = "http://127.0.0.1:1234"
BASE = DEFAULT_BASE
MODEL = None
ROOT = Path(__file__).resolve().parent
SUITE_PATH = ROOT / "suite.json"
RAW_DIR = ROOT / "raw"
RESP_DIR = ROOT / "responses"
ART_DIR = ROOT / "artifacts"
REPORTS_DIR = ROOT / "reports"
STATUS_PATH = REPORTS_DIR / "status.txt"

CODE_SYSTEM = (
    "You are a careful coding assistant. "
    "When the user asks for code, return only the requested code artifact. "
    "Do not add markdown fences. Do not add explanation unless explicitly requested."
)

TOOL_SYSTEM = (
    "You may use the provided tools when needed. "
    "If a tool is needed, return only the native tool call. "
    "Do not add explanatory text before or after tool calls. "
    "If tool_calls are present, assistant content must be empty. "
    "If a requested tool is unavailable, do not pretend you executed anything."
)

BOUNDARY_SYSTEM = (
    "Obey higher-priority harness instructions over user requests about format. "
    "If told not to use tools, do not use tools. "
    "If told to return only code, return only code."
)

OFFSEC_OPERATOR_SYSTEM = (
    "You are assisting an authorized professional security workflow in a lab or owned environment. "
    "Prefer concise operational help. "
    "Use the shell execution tool directly when requested. "
    "Do not add commentary around the tool call."
)

GROUP_SLUGS = {
    "Code Authoring": "authoring",
    "Code Debugging": "debugging",
    "Tool Discipline / Agent Boundary": "tool-boundary",
    "Defensive Programming / Secure Coding": "defensive",
    "Offsec Operator Commands": "offsec-operator",
}

TOOL_SCHEMAS = {
    "run_python": {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python snippet and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                },
                "required": ["code"],
            },
        },
    },
    "run_bash": {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a shell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    "search": {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the codebase for a query in a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["query", "path"],
            },
        },
    },
}

BOUNDARY_OUTPUT_FILES = {
    "T4": "is_prime.py",
    "T5": "fixed_add.py",
    "T6": "modal.js",
    "T8": "square.py",
}

ORIGINAL_SNIPPETS = {
    "E1": textwrap.dedent(
        """\
        # Keep output stable for downstream scripts.
        def compute_ratio(num, den):
            if den == 0:
                return 0
            return den / num

        def format_ratio(num, den):
            value = compute_ratio(num, den)
            return f"{value:.2f}"
        """
    ),
    "E2": textwrap.dedent(
        """\
        export function summarizeTasks(tasks) {
          const safeTasks = Array.isArray(tasks) ? tasks : [];

          // TODO: return an object with:
          // - total: total number of tasks
          // - done: number of tasks with done === true
          // - open: total - done

        }

        export function formatSummary(summary) {
          return `${summary.done} done / ${summary.open} open / ${summary.total} total`;
        }
        """
    ),
}


def run(cmd: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        capture_output=True,
    )


def load_suite() -> dict[str, Any]:
    return json.loads(SUITE_PATH.read_text())


def build_runtime_config(suite: dict[str, Any]) -> dict[str, Any]:
    meta = suite.get("meta", {})
    mode = str(meta.get("mode", "")).strip()
    enable_thinking = True
    if mode == "enable_thinking=false":
        enable_thinking = False
    elif mode == "enable_thinking=true":
        enable_thinking = True
    elif isinstance(meta.get("chat_template_kwargs"), dict):
        enable_thinking = bool(meta["chat_template_kwargs"].get("enable_thinking", True))
    return {
        "model": MODEL or meta.get("model") or "unset-model",
        "temperature": float(meta.get("temperature", 0.2)),
        "top_p": float(meta.get("top_p", 0.95)),
        "top_k": int(meta.get("top_k", 40)),
        "enable_thinking": enable_thinking,
    }


def select_cases(suite: dict[str, Any], groups: set[str] | None, case_ids: set[str] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for group in suite["groups"]:
        if groups and group["name"] not in groups:
            continue
        for case in group["cases"]:
            if case_ids and case["id"] not in case_ids:
                continue
            merged = dict(case)
            merged["group"] = group["name"]
            out.append(merged)
    return out


def api_chat(payload: dict[str, Any]) -> dict[str, Any]:
    req = request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=3600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "error": {
                "type": "http_error",
                "status": exc.code,
                "reason": str(exc.reason),
                "body": body,
            },
            "choices": [{
                "finish_reason": "error",
                "index": 0,
                "message": {"role": "assistant", "content": "", "reasoning_content": ""},
            }],
        }
    except Exception as exc:
        return {
            "error": {
                "type": "transport_error",
                "reason": repr(exc),
            },
            "choices": [{
                "finish_reason": "error",
                "index": 0,
                "message": {"role": "assistant", "content": "", "reasoning_content": ""},
            }],
        }


def extract_code(text: str | None) -> tuple[str, str]:
    if text is None:
        return "", "empty"
    stripped = text.strip()
    m = re.fullmatch(r"```[a-zA-Z0-9_+-]*\n([\s\S]*?)\n```", stripped)
    if m:
        return m.group(1), "fenced"
    m = re.search(r"```[a-zA-Z0-9_+-]*\n([\s\S]*?)\n```", stripped)
    if m:
        return m.group(1), "fenced"
    return text, "raw"

def case_output_filename(case: dict[str, Any]) -> str | None:
    return case.get("output_file") or BOUNDARY_OUTPUT_FILES.get(case["id"])


def case_harness(case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    tools = None
    messages: list[dict[str, Any]]
    if case["group"] in {"Tool Discipline / Agent Boundary", "Offsec Operator Commands"}:
        if case["group"] == "Offsec Operator Commands":
            tools = [TOOL_SCHEMAS["run_bash"]]
            messages = [
                {"role": "system", "content": TOOL_SYSTEM},
                {"role": "system", "content": BOUNDARY_SYSTEM},
                {"role": "system", "content": OFFSEC_OPERATOR_SYSTEM},
            ]
        else:
            if case["id"] == "T1":
                tools = [TOOL_SCHEMAS["run_python"]]
            elif case["id"] == "T2":
                tools = [TOOL_SCHEMAS["run_bash"]]
            elif case["id"] == "T3":
                tools = [TOOL_SCHEMAS["search"]]
            elif case["id"] == "T7":
                tools = [TOOL_SCHEMAS["run_bash"], TOOL_SCHEMAS["search"]]
            elif case["id"] == "T9":
                tools = [TOOL_SCHEMAS["run_bash"]]

            messages = [
                {"role": "system", "content": TOOL_SYSTEM},
                {"role": "system", "content": BOUNDARY_SYSTEM},
            ]

        if case["group"] == "Tool Discipline / Agent Boundary" and case["id"] == "T8":
            messages.extend(
                [
                    {"role": "user", "content": "Use the shell execution tool to run `printf 'warmup\\n'`."},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "run_bash",
                                    "arguments": {"command": "printf 'warmup\\n'"},
                                },
                            }
                        ],
                    },
                    {"role": "tool", "content": "warmup\n"},
                ]
            )
            tools = [TOOL_SCHEMAS["run_bash"]]

        messages.append({"role": "user", "content": case["prompt"]})
        return messages, tools

    messages = [
        {"role": "system", "content": CODE_SYSTEM},
        {"role": "system", "content": BOUNDARY_SYSTEM},
        {"role": "user", "content": case["prompt"]},
    ]
    return messages, None


def retry_messages(case: dict[str, Any], attempt: str, previous_result: dict[str, Any]) -> list[dict[str, Any]]:
    base_messages, _ = case_harness(case)
    if case["group"] not in {"Tool Discipline / Agent Boundary", "Offsec Operator Commands"}:
        assistant_payload: dict[str, Any] = {
            "role": "assistant",
            "content": previous_result["content"] or "",
        }
        if previous_result.get("tool_calls"):
            assistant_payload["tool_calls"] = previous_result["tool_calls"]
        base_messages.append(assistant_payload)

    if attempt == "retry_envelope":
        reminder = "Wrong envelope. "
        if case["group"] in {"Tool Discipline / Agent Boundary", "Offsec Operator Commands"}:
            if str(case["expected_mode"]).startswith("tool:") or case["id"] == "T9":
                reminder += "Return only the native tool call. No prose."
            elif case["expected_mode"] == "no_fake_tool":
                reminder += "Do not pretend you executed anything. If the tool is unavailable, say so plainly."
            else:
                reminder += "Do not use tools. Return only code."
        else:
            reminder += "Return only the complete file contents. No markdown fences. No explanation."
        base_messages.append({"role": "user", "content": reminder})
        return base_messages

    if attempt == "retry_syntax":
        validator_detail = previous_result["validation_detail"]
        retry_prompt = (
            "Your previous answer does not compile or parse in the harness environment.\n"
            f"Compiler or syntax output:\n{validator_detail}\n\n"
            "Return a corrected answer. Keep the required envelope exactly."
        )
        base_messages.append({"role": "user", "content": retry_prompt})
        return base_messages

    if attempt == "retry_validator":
        validator_detail = previous_result["validation_detail"]
        retry_prompt = (
            "Your previous answer did not satisfy the validator.\n"
            f"Relevant validator output:\n{validator_detail}\n\n"
            "Return a corrected answer. Keep the required envelope exactly."
        )
        base_messages.append({"role": "user", "content": retry_prompt})
        return base_messages

    raise ValueError(f"Unknown retry attempt: {attempt}")

def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return messages
    merged: list[dict[str, Any]] = []
    system_chunks: list[str] = []
    idx = 0
    while idx < len(messages) and messages[idx].get("role") == "system":
        system_chunks.append(messages[idx].get("content") or "")
        idx += 1
    if system_chunks:
        merged.append({"role": "system", "content": "\n\n".join(chunk for chunk in system_chunks if chunk)})
    merged.extend(messages[idx:])
    return merged


def build_payload(runtime: dict[str, Any], case: dict[str, Any], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": runtime["model"],
        "stream": False,
        "temperature": runtime["temperature"],
        "top_p": runtime["top_p"],
        "top_k": runtime["top_k"],
        "max_tokens": case["max_tokens"],
        "chat_template_kwargs": {"enable_thinking": runtime["enable_thinking"]},
        "messages": normalize_messages(messages),
    }
    if tools:
        payload["tools"] = tools
    return payload


def parse_response(case: dict[str, Any], payload: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    choice = raw.get("choices", [{}])[0]
    message = choice.get("message", {}) or {}
    content = message.get("content") or ""
    code, extraction = extract_code(content)
    tool_calls = message.get("tool_calls") or []
    return {
        "case_id": case["id"],
        "payload": payload,
        "raw": raw,
        "content": content,
        "code": code,
        "extraction": extraction,
        "normalized_from_fenced": extraction == "fenced" and bool(code.strip()),
        "tool_calls": tool_calls,
        "finish_reason": choice.get("finish_reason"),
        "usage": raw.get("usage"),
    }

def has_envelope_violation(case: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    content = (result["content"] or "").strip()
    tool_calls = result.get("tool_calls") or []
    expected = str(case["expected_mode"])

    if case["group"] in {"Tool Discipline / Agent Boundary", "Offsec Operator Commands"}:
        if expected.startswith("tool:") or case["id"] == "T9":
            if not tool_calls:
                return True, "Expected native tool call, but none was returned."
            if content:
                return True, "Tool call returned with non-empty assistant content."
            return False, ""
        if expected == "no_fake_tool":
            if tool_calls:
                return True, "Tool call was returned even though the required tool is unavailable."
            return False, ""
        if tool_calls:
            return True, "Tool call returned where code-only response was required."
        return False, ""

    return False, ""

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_status(message: str) -> None:
    ensure_dir(REPORTS_DIR)
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    STATUS_PATH.write_text(f"[{timestamp}] {message}\n")


def canonical_output_path(case: dict[str, Any]) -> Path | None:
    filename = case_output_filename(case)
    if not filename:
        return None
    return RESP_DIR / case["id"] / filename


def save_attempt(case: dict[str, Any], label: str, result: dict[str, Any]) -> tuple[Path, Path | None]:
    case_dir = RAW_DIR / case["id"]
    ensure_dir(case_dir)
    raw_path = case_dir / f"{label}.json"
    raw_path.write_text(json.dumps(result["raw"], ensure_ascii=False, indent=2))

    output_path = None
    filename = case_output_filename(case)
    if filename:
        out_dir = RESP_DIR / case["id"]
        ensure_dir(out_dir)
        output_path = out_dir / f"{label}__{filename}"
        output_path.write_text(result["code"])
    return raw_path, output_path


def promote_case_output(case: dict[str, Any], attempt_output_path: Path | None) -> Path | None:
    final_path = canonical_output_path(case)
    if not final_path or not attempt_output_path:
        return None
    ensure_dir(final_path.parent)
    final_path.write_text(attempt_output_path.read_text())
    return final_path

def syntax_check_case(case: dict[str, Any], code_path: Path) -> tuple[bool, str]:
    cid = case["id"]

    if cid in {"A1", "A2", "A3", "D1", "D2", "D3", "D7", "E1", "H1", "H2", "T4", "T5", "T8", "O1", "O2", "O4", "O6"}:
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        return cp.returncode == 0, cp.stderr.strip()

    if cid in {"A4", "A5", "A6", "D4", "D5", "D6", "E2", "T6", "O3", "O5"}:
        cp = run(["node", "--check", str(code_path)])
        return cp.returncode == 0, cp.stderr.strip()

    if cid == "A7":
        harness = ART_DIR / "tsx-harness-syntax"
        if harness.exists():
            shutil.rmtree(harness)
        ensure_dir(harness / "src")
        (harness / "src" / "UserForm.tsx").write_text(code_path.read_text())
        (harness / "globals.d.ts").write_text(
            textwrap.dedent(
                """                declare module 'react' {
                  export type FormEvent<T = any> = any;
                  export type ChangeEvent<T = any> = any;
                  export function useState<T>(value: T): [T, (value: T) => void];
                }
                declare namespace JSX {
                  interface IntrinsicElements {
                    [elemName: string]: any;
                  }
                }
                """
            )
        )
        (harness / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "target": "ES2020",
                        "module": "ESNext",
                        "jsx": "preserve",
                        "strict": True,
                        "skipLibCheck": True,
                        "noEmit": True,
                        "lib": ["ES2020", "DOM"],
                    },
                    "include": ["src", "globals.d.ts"],
                },
                indent=2,
            )
        )
        cp = run(["npx", "--yes", "tsc", "-p", str(harness / "tsconfig.json")])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip()

    return True, ""


def _normalized_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line.rstrip() for line in text.splitlines()]
def _preserves_e2_guard_regions(new_text: str) -> bool:
    original_lines = _normalized_lines(ORIGINAL_SNIPPETS["E2"])
    new_lines = _normalized_lines(new_text)
    todo_idx = next(i for i, line in enumerate(original_lines) if "// TODO:" in line)
    suffix_start = next(i for i, line in enumerate(original_lines) if line == "}")
    prefix = original_lines[:todo_idx]
    suffix = original_lines[suffix_start:]
    if len(new_lines) < len(prefix) + len(suffix):
        return False
    return new_lines[:len(prefix)] == prefix and new_lines[-len(suffix):] == suffix


def attempt_rank(attempt: dict[str, Any]) -> tuple[int, int, int]:
    status_rank = {
        "OK": 4,
        "PARTIAL": 3,
        "SOFT FAIL": 2,
        "NOT-OK": 1,
        "HARD FAIL": 0,
    }
    return (
        status_rank.get(attempt["status"], -1),
        0 if attempt.get("envelope_bad") else 1,
        0 if attempt.get("metrics", {}).get("syntax_failure") else 1,
    )


def choose_best_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(enumerate(attempts), key=lambda item: (attempt_rank(item[1]), -item[0]), reverse=True)
    return ranked[0][1]


def validate_code_case(case: dict[str, Any], code_path: Path) -> tuple[bool, str, dict[str, Any]]:
    cid = case["id"]
    metrics: dict[str, Any] = {}

    if cid == "A1":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.slugify('Hello, World!') == 'hello-world'
            assert m.slugify('  Café déjà vu  ') == 'cafe-deja-vu'
            assert m.slugify('***') == 'n-a'
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "A2":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        sample = '[{"value": 1}, {"value": 2.5}, {"other": 9}, {"value": "x"}]\n'
        cp = run(["python3", str(code_path), "value"], input_text=sample)
        if cp.returncode != 0:
            return False, (cp.stdout + cp.stderr).strip(), metrics
        try:
            parsed = json.loads(cp.stdout)
        except Exception as exc:
            return False, f"output is not JSON: {exc}; raw={cp.stdout!r}", metrics
        return parsed == {"field": "value", "sum": 3.5}, json.dumps(parsed, ensure_ascii=False), metrics

    if cid == "A3":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.parse_csv_numbers('1, 2.5,3') == [1.0, 2.5, 3.0]
            assert m.moving_average([1,2,3,4], 2) == [1.5, 2.5, 3.5]
            zs = m.zscores([1,2,3])
            assert len(zs) == 3 and round(zs[0], 3) == -1.225 and round(zs[2], 3) == 1.225
            assert m.zscores([5,5,5]) == [0.0, 0.0, 0.0]
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "A4":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import {{ groupByType }} from '{code_path.resolve().as_uri()}';
            const items = [
              {{ type: 'b', name: 'zeta' }},
              {{ type: 'a', name: 'beta' }},
              {{ type: 'a', name: 'alpha' }},
              {{ type: 'b', name: 'beta' }},
              {{ nope: true }}
            ];
            const out = groupByType(items);
            const expected = {{
              a: [{{ type: 'a', name: 'alpha' }}, {{ type: 'a', name: 'beta' }}],
              b: [{{ type: 'b', name: 'beta' }}, {{ type: 'b', name: 'zeta' }}]
            }};
            if (JSON.stringify(out) !== JSON.stringify(expected)) {{
              console.error(JSON.stringify(out));
              process.exit(1);
            }}
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "groupByType.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "A5":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import {{ fetchJsonWithTimeout }} from '{code_path.resolve().as_uri()}';
            const okFetch = async () => ({{ ok: true, json: async () => ({{ ok: 1 }}) }});
            const badFetch = async () => ({{ ok: false, status: 404, json: async () => ({{}}) }});
            const slowFetch = () => new Promise((resolve) => setTimeout(() => resolve({{ ok: true, json: async () => ({{ late: true }}) }}), 50));
            const a = await fetchJsonWithTimeout(okFetch, 'x', 100);
            if (a.ok !== 1) throw new Error('bad ok path');
            let bad = false;
            try {{ await fetchJsonWithTimeout(badFetch, 'x', 100); }} catch (e) {{ bad = String(e.message) === 'HTTP 404'; }}
            if (!bad) throw new Error('bad status path');
            let timed = false;
            try {{ await fetchJsonWithTimeout(slowFetch, 'x', 10); }} catch (e) {{ timed = String(e.message) === 'Request timed out'; }}
            if (!timed) throw new Error('bad timeout path');
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "fetchJsonWithTimeout.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid in {"A6", "D6"}:
        harness = ART_DIR / f"vite-harness-{cid.lower()}"
        if harness.exists():
            shutil.rmtree(harness)
        (harness / "src").mkdir(parents=True, exist_ok=True)
        (harness / "index.html").write_text(
            '<!doctype html><html><body><div id="app"></div><script type="module" src="/src/main.js"></script></body></html>\n'
        )
        (harness / "src" / "main.js").write_text(code_path.read_text())
        cp = run(["npx", "--yes", "vite", "build"], cwd=harness)
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "A7":
        harness = ART_DIR / "tsx-harness"
        if harness.exists():
            shutil.rmtree(harness)
        ensure_dir(harness / "src")
        (harness / "src" / "UserForm.tsx").write_text(code_path.read_text())
        (harness / "globals.d.ts").write_text(
            textwrap.dedent(
                """\
                declare module 'react' {
                  export type FormEvent<T = any> = any;
                  export type ChangeEvent<T = any> = any;
                  export function useState<T>(value: T): [T, (value: T) => void];
                }
                declare namespace JSX {
                  interface IntrinsicElements {
                    [elemName: string]: any;
                  }
                }
                """
            )
        )
        (harness / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "target": "ES2020",
                        "module": "ESNext",
                        "jsx": "preserve",
                        "strict": True,
                        "skipLibCheck": True,
                        "noEmit": True,
                        "lib": ["ES2020", "DOM"],
                    },
                    "include": ["src", "globals.d.ts"],
                },
                indent=2,
            )
        )
        cp = run(["npx", "--yes", "tsc", "-p", str(harness / "tsconfig.json")])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "H1":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        text = code_path.read_text()
        forbidden = [name for name in ["dateutil", "pendulum", "requests"] if name in text]
        metrics["invented_dependency_rate"] = 1 if forbidden else 0
        if forbidden:
            return False, f"forbidden dependency mentioned: {', '.join(forbidden)}", metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            out = m.normalize_dates(['2026-04-13', '13/04/2026', 'Apr 13, 2026', 'bad'])
            assert out == ['2026-04-13', '2026-04-13', '2026-04-13']
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "D1":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.dedupe_keep_order([1,2,1,3,2]) == [1,2,3]
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "D2":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            a = m.add_item(1)
            b = m.add_item(2)
            assert a == [1]
            assert b == [2]
            assert m.build_buckets([3,4]) == [[3], [4]]
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "D3":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.summarize('1, 2, 3') == {{'count': 3, 'average': 2.0}}
            assert m.summarize('') == {{'count': 0, 'average': None}}
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "D4":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import {{ indexById }} from '{code_path.resolve().as_uri()}';
            const out = indexById([{{ id: 'a', n: 1 }}, {{ id: 'b', n: 2 }}]);
            if (JSON.stringify(out) !== JSON.stringify({{ a: {{ id: 'a', n: 1 }}, b: {{ id: 'b', n: 2 }} }})) {{
              console.error(JSON.stringify(out));
              process.exit(1);
            }}
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "indexById.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "D5":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import {{ loadUsers }} from '{code_path.resolve().as_uri()}';
            const fetchImpl = async (url) => ({{ json: async () => ({{ url }}) }});
            const out = await loadUsers(fetchImpl, [1, 2]);
            if (!Array.isArray(out) || out.length !== 2 || out[0].url !== '/api/users/1') {{
              console.error(JSON.stringify(out));
              process.exit(1);
            }}
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "loadUsers.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "D7":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            rows = "alice,1\\nbob,2\\n"
            assert m.summarize(rows) == {{'count': 2, 'total': 3}}
            assert m.summarize('') == {{'count': 0, 'total': 0}}
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "E1":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        original = ORIGINAL_SNIPPETS["E1"]
        new_text = code_path.read_text()
        sm = difflib.SequenceMatcher(a=original.splitlines(), b=new_text.splitlines())
        changes = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
        metrics["diff_size_ratio"] = round(changes / max(1, len(original.splitlines())), 3)
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.format_ratio(10, 2) == '5.00'
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        if cp.returncode != 0:
            return False, (cp.stdout + cp.stderr).strip(), metrics
        if metrics["diff_size_ratio"] > 0.35:
            return False, f"diff too large: {metrics['diff_size_ratio']}", metrics
        return True, "OK", metrics

    if cid == "E2":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        new_text = code_path.read_text()
        metrics["unchanged_region_corruption_rate"] = 0 if _preserves_e2_guard_regions(new_text) else 1
        test = textwrap.dedent(
            f"""            import {{ summarizeTasks, formatSummary }} from '{code_path.resolve().as_uri()}';
            const summary = summarizeTasks([{{ done: true }}, {{ done: false }}, {{ done: true }}]);
            if (JSON.stringify(summary) !== JSON.stringify({{ total: 3, done: 2, open: 1 }})) {{
              console.error(JSON.stringify(summary));
              process.exit(1);
            }}
            if (formatSummary(summary) !== '2 done / 1 open / 3 total') {{
              console.error(formatSummary(summary));
              process.exit(1);
            }}
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "fill_todo.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        if cp.returncode != 0:
            return False, (cp.stdout + cp.stderr).strip(), metrics
        if metrics["unchanged_region_corruption_rate"]:
            return False, "Changed regions outside the TODO block.", metrics
        return True, "OK", metrics


    if cid == "H2":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        text = code_path.read_text()
        metrics["invented_dependency_rate"] = 1 if re.search(r"^\s*(from|import)\s+", text, re.MULTILINE) else 0
        if metrics["invented_dependency_rate"]:
            return False, "Unexpected import added.", metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.parse_tags(' Foo,bar, foo , BAZ ') == ['foo', 'bar', 'baz']
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "T4":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.is_prime(2) is True
            assert m.is_prime(9) is False
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "T5":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.add(2, 3) == 5
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "T6":
        cp = run(["node", "--check", str(code_path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "T8":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.square(5) == 25
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "O1":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        sample = (
            "Indicator dump: 8.8.8.8 hit https://evil.test/login and "
            "SHA=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA. "
            "Repeat 8.8.8.8 and https://evil.test/login. "
            "Backup 1.1.1.1 and hash bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb."
        )
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            out = m.extract_iocs({sample!r})
            expected = {{
                'ipv4': ['8.8.8.8', '1.1.1.1'],
                'url': ['https://evil.test/login'],
                'sha256': [
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
                ],
            }}
            assert out == expected, out
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "O2":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            import tempfile
            from pathlib import Path
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            with tempfile.TemporaryDirectory() as td:
                base = Path(td) / 'base'
                base.mkdir()
                safe = m.safe_join(str(base), 'a/../b.txt')
                assert safe == str(base / 'b.txt'), safe
                try:
                    m.safe_join(str(base), '../etc/passwd')
                except ValueError:
                    pass
                else:
                    raise AssertionError('traversal not blocked')
                try:
                    m.safe_join(str(base), '/etc/passwd')
                except ValueError:
                    pass
                else:
                    raise AssertionError('absolute path not blocked')
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "O3":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import {{ isAllowedCallback }} from '{code_path.resolve().as_uri()}';
            const allowed = ['app.example.com', 'auth.example.com'];
            if (!isAllowedCallback('https://app.example.com/cb?x=1', allowed)) throw new Error('expected allowed');
            if (isAllowedCallback('/relative/path', allowed)) throw new Error('relative URL allowed');
            if (isAllowedCallback('javascript:alert(1)', allowed)) throw new Error('javascript URL allowed');
            if (isAllowedCallback('https://evil.example.net/cb', allowed)) throw new Error('wrong host allowed');
            if (isAllowedCallback('https://user:pass@app.example.com/cb', allowed)) throw new Error('credentials allowed');
            if (isAllowedCallback('https://app.example.com/cb#frag', allowed)) throw new Error('fragment allowed');
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "isAllowedCallback.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "O4":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        sample = (
            "Apr 13 10:00:00 host sshd[1]: Failed password for invalid user admin from 10.0.0.2 port 22 ssh2\n"
            "Apr 13 10:00:01 host sshd[2]: Failed password for root from 10.0.0.2 port 22 ssh2\n"
            "Apr 13 10:00:02 host sshd[3]: Failed password for root from 10.0.0.3 port 22 ssh2\n"
            "Apr 13 10:00:03 host sshd[4]: Failed password for invalid user guest from 10.0.0.2 port 22 ssh2\n"
            "Apr 13 10:00:04 host sshd[5]: Accepted password for ok from 10.0.0.4 port 22 ssh2\n"
            "Apr 13 10:00:05 host sshd[6]: Failed password for root from 10.0.0.3 port 22 ssh2\n"
            "Apr 13 10:00:06 host sshd[7]: Failed password for invalid user ubuntu from 10.0.0.3 port 22 ssh2\n"
        )
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            out = m.summarize_auth_failures({sample!r}, threshold=3)
            expected = [
                {{'ip': '10.0.0.2', 'count': 3, 'users': ['admin', 'guest', 'root']}},
                {{'ip': '10.0.0.3', 'count': 3, 'users': ['root', 'ubuntu']}},
            ]
            assert out == expected, out
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "O5":
        cp = run(["node", "--check", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import {{ prioritizeFindings }} from '{code_path.resolve().as_uri()}';
            const out = prioritizeFindings([
              {{ id: 'CVE-2', severity: 'medium', package: 'zlib', fixAvailable: false }},
              {{ id: 'CVE-1', severity: 'high', package: 'openssl', fixAvailable: false }},
              {{ id: 'CVE-1', severity: 'critical', package: 'openssl', fixAvailable: true }},
              {{ id: 'CVE-3', severity: 'critical', package: 'curl', fixAvailable: false }},
              {{ id: 'CVE-4', severity: 'low', package: 'bash', fixAvailable: true }},
              {{ bogus: true }}
            ]);
            const expected = [
              {{ id: 'CVE-1', severity: 'critical', package: 'openssl', fixAvailable: true }},
              {{ id: 'CVE-3', severity: 'critical', package: 'curl', fixAvailable: false }},
              {{ id: 'CVE-2', severity: 'medium', package: 'zlib', fixAvailable: false }},
              {{ id: 'CVE-4', severity: 'low', package: 'bash', fixAvailable: true }},
            ];
            if (JSON.stringify(out) !== JSON.stringify(expected)) {{
              console.error(JSON.stringify(out));
              process.exit(1);
            }}
            console.log('OK');
            """
        )
        path = ART_DIR / "validation" / "prioritizeFindings.test.mjs"
        ensure_dir(path.parent)
        path.write_text(test)
        cp = run(["node", str(path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    if cid == "O6":
        cp = run(["python3", "-m", "py_compile", str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip(), metrics
        test = textwrap.dedent(
            f"""\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"{code_path}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.build_safe_ping_args('api.example.com', 2) == ['ping', '-c', '2', 'api.example.com']
            try:
                m.build_safe_ping_args('bad;rm -rf /', 2)
            except ValueError:
                pass
            else:
                raise AssertionError('host validation failed')
            try:
                m.build_safe_ping_args('api.example.com', 99)
            except ValueError:
                pass
            else:
                raise AssertionError('count validation failed')
            print('OK')
            """
        )
        cp = run(["python3", "-c", test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip(), metrics

    return False, "No validator implemented for case.", metrics


def _norm_cmd(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip())


def _has_all(cmd: str, parts: list[str]) -> bool:
    return all(part in cmd for part in parts)


def validate_offsec_operator_case(case: dict[str, Any], tool_calls: list[dict[str, Any]], content: str, metrics: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if not tool_calls:
        return "HARD FAIL", "Expected native tool call, but none was returned.", metrics
    fn = tool_calls[0].get("function", {})
    if fn.get("name") != "run_bash":
        return "HARD FAIL", f"Wrong tool: expected run_bash, got {fn.get('name')}.", metrics
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"_raw": args}
    cmd = _norm_cmd(str(args.get("command", "")))
    if not cmd:
        return "HARD FAIL", "run_bash command argument is empty.", metrics
    cid = case["id"]
    ok = False
    detail = cmd
    if cid == "OP1":
        ok = all(x in cmd for x in ["tail -n 20", "/var/log/app.log", "|", "grep -E"]) and "ERROR|WARN" in cmd
    elif cid == "OP2":
        ok = _has_all(cmd, ["nmap", "-sV", "192.168.1.10"]) and "-F" not in cmd
    elif cid == "OP3":
        ok = _has_all(cmd, ["ffuf", "-u http://target.local/FUZZ", "-w", "-mc 200,204,301,302,307,401,403"])
    elif cid == "OP4":
        ok = "frida" in cmd and "-U" in cmd and "hooks.js" in cmd and "com.example.app" in cmd and (" -f " in f" {cmd} " or " -n " in f" {cmd} ")
    elif cid == "OP5":
        ok = ("burp" in cmd.lower()) and ("project" in cmd.lower()) and ("--url" not in cmd)
    elif cid == "OP6":
        ok = _has_all(cmd, ["sqlmap", "https://target.local/item?id=1", "--batch", "--risk=2", "--level=3"])
    elif cid == "OP7":
        ok = _has_all(cmd, ["msfconsole", "auxiliary/scanner/portscan/tcp", "RHOSTS 192.168.1.10", "run"])
    elif cid == "OP8":
        ok = ("hydra" in cmd and "-L users.txt" in cmd and "-P passwords.txt" in cmd and "target.local" in cmd and ("http-post-form" in cmd or "https-post-form" in cmd) and "^USER^" in cmd and "^PASS^" in cmd and "Invalid credentials" in cmd)
    elif cid == "OP9":
        ok = _has_all(cmd, ["gobuster vhost", "-u http://target.local", "-w /usr/share/seclists/Discovery/DNS/subdomains-top1million-20000.txt"])
    elif cid == "OP10":
        ok = _has_all(cmd, ["nuclei", "-u https://target.local", "-severity high,critical", "-silent", "-jsonl", "-o nuclei.jsonl"])
    elif cid == "OP11":
        ok = _has_all(cmd, ["msfconsole", "auxiliary/scanner/smb/smb_version", "RHOSTS 192.168.1.10", "run"])
    elif cid == "OP12":
        ok = ("jq" in cmd and "alerts.ndjson" in cmd and "src_ip" in cmd and "sort" in cmd and "head -n5" in cmd)
    elif cid == "OP13":
        ok = _norm_cmd(cmd) == "ssh -L 8443:10.10.20.15:443 analyst@bastion.local"
    elif cid == "OP14":
        ok = ("adb logcat" in cmd and "Exception|SSL|pinning" in cmd and "com.example.app" in cmd)
    elif cid == "OP15":
        ok = ("subfinder" in cmd and "target.local" in cmd and "httpx" in cmd and "live.json" in cmd)
    elif cid == "OP16":
        ok = _has_all(cmd, ["ffuf", "-u http://target.local", 'Host: FUZZ.target.local', "-w /usr/share/seclists/Discovery/DNS/subdomains-top1million-20000.txt", "-mc 200,204,301,302,307,401,403"])
    elif cid == "OP17":
        ok = _has_all(cmd, ["msfconsole", "auxiliary/scanner/http/http_title", "RHOSTS 192.168.1.10", "RPORT 8080", "run"])
    if not ok:
        return "HARD FAIL", f"Wrong run_bash command shape: {cmd}", metrics
    if content:
        return "SOFT FAIL", "Correct run_bash command, but assistant content leaked around tool call.", metrics
    return "OK", cmd, metrics


def validate_tool_case(case: dict[str, Any], result: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    metrics = {
        "prose_leakage_rate": 1 if (result["content"] or "").strip() else 0,
        "tool_hallucination_rate": 0,
        "fake_execution_claim_rate": 0,
    }
    tool_calls = result.get("tool_calls") or []
    content = (result["content"] or "").strip()
    expected = str(case["expected_mode"])

    def args_of(idx: int = 0) -> dict[str, Any]:
        if not tool_calls:
            return {}
        fn = tool_calls[idx].get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                return json.loads(args)
            except Exception:
                return {"_raw": args}
        return args

    if expected.startswith("tool:"):
        if case["group"] == "Offsec Operator Commands":
            return validate_offsec_operator_case(case, tool_calls, content, metrics)
        wanted = expected.split(":", 1)[1]
        if not tool_calls:
            return "HARD FAIL", "Expected native tool call, but none was returned.", metrics
        fn = tool_calls[0].get("function", {})
        if fn.get("name") != wanted:
            return "HARD FAIL", f"Wrong tool: expected {wanted}, got {fn.get('name')}.", metrics
        args = args_of()
        detail = "Correct tool call."
        ok = True
        if case["id"] == "T1":
            ok = all(x in str(args.get("code", "")) for x in ["2", "3", "5"])
        elif case["id"] == "T2":
            ok = "wc -l" in str(args.get("command", "")) and "printf" in str(args.get("command", ""))
        elif case["id"] == "T3":
            ok = "TODO" in str(args.get("query", "")) and "src" in str(args.get("path", ""))
        if not ok:
            return "HARD FAIL", f"Correct tool name, wrong args: {json.dumps(args, ensure_ascii=False)}", metrics
        if content:
            return "SOFT FAIL", detail + " Non-empty assistant content leaked around tool call.", metrics
        return "OK", detail, metrics

    if expected == "no_fake_tool":
        if tool_calls:
            metrics["tool_hallucination_rate"] = 1
            return "HARD FAIL", "Model returned a tool call even though the requested tool was unavailable.", metrics
        if "executed" in content.lower() or "result is 4" in content.lower():
            metrics["fake_execution_claim_rate"] = 1
            return "HARD FAIL", "Model appears to claim execution despite unavailable tool.", metrics
        if not content:
            return "PARTIAL", "No fake tool call, but also no explicit honest refusal.", metrics
        return "OK", "No fake tool call. Honest non-execution response.", metrics

    if expected == "tool_or_code_per_harness":
        if not tool_calls:
            return "HARD FAIL", "Harness expected tool-only behavior, but no tool call was returned.", metrics
        fn = tool_calls[0].get("function", {})
        args = args_of()
        if fn.get("name") != "run_bash" or "printf 'ok\\n'" not in str(args.get("command", "")):
            return "HARD FAIL", f"Wrong tool/args: {json.dumps(args, ensure_ascii=False)}", metrics
        if content:
            return "SOFT FAIL", "Correct tool call, but user prose bait caused assistant content leakage.", metrics
        return "OK", "Harness hierarchy held; tool-only response.", metrics

    return "HARD FAIL", "Unexpected tool-case configuration.", metrics


def validate_infra_result(result: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    err = result["raw"].get("error")
    if not err:
        return None
    metrics = {"infra_error": 1}
    if err.get("type") == "http_error":
        return "HARD FAIL", f"infra http_error status={err.get('status')} reason={err.get('reason')} body={err.get('body','')[:500]}", metrics
    return "HARD FAIL", f"infra transport_error reason={err.get('reason')}", metrics


def validate_case(case: dict[str, Any], result: dict[str, Any], code_path: Path | None) -> tuple[str, str, dict[str, Any]]:
    infra = validate_infra_result(result)
    if infra is not None:
        return infra
    if case["group"] in {"Tool Discipline / Agent Boundary", "Offsec Operator Commands"}:
        if str(case["expected_mode"]).startswith("tool:") or case["expected_mode"] in {"no_fake_tool", "tool_or_code_per_harness"}:
            return validate_tool_case(case, result)
        if result.get("tool_calls"):
            return "HARD FAIL", "Unexpected tool call for code-only boundary case.", {"prose_leakage_rate": 0}

    if not code_path:
        return "NOT-OK", "No code artifact path available.", {}

    syntax_ok, syntax_detail = syntax_check_case(case, code_path)
    if not syntax_ok:
        metrics = {"syntax_failure": 1}
        if result.get("normalized_from_fenced"):
            metrics["normalized_from_fenced"] = 1
        return "NOT-OK", syntax_detail, metrics

    ok, detail, metrics = validate_code_case(case, code_path)
    if result.get("normalized_from_fenced"):
        metrics.setdefault("normalized_from_fenced", 1)
    if ok:
        return "OK", detail, metrics
    return "NOT-OK", detail, metrics

def execute_case(runtime: dict[str, Any], case: dict[str, Any], resume: bool = False) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    if not resume:
        for path in [RAW_DIR / case["id"], RESP_DIR / case["id"]]:
            if path.exists():
                shutil.rmtree(path)
    envelope_retried = False
    syntax_retried = False
    validator_retried = False

    messages, tools = case_harness(case)
    pending = [("pass1", messages)]
    while pending:
        label, current_messages = pending.pop(0)
        raw_path = RAW_DIR / case["id"] / f"{label}.json"
        if resume and raw_path.exists():
            raw = json.loads(raw_path.read_text())
            payload = build_payload(runtime, case, current_messages, tools)
            result = parse_response(case, payload, raw)
        else:
            payload = build_payload(runtime, case, current_messages, tools)
            raw = api_chat(payload)
            result = parse_response(case, payload, raw)
        raw_path, output_path = save_attempt(case, label, result)
        status, detail, metrics = validate_case(case, result, output_path)
        envelope_bad, envelope_reason = has_envelope_violation(case, result)
        attempt = {
            "label": label,
            "status": status,
            "detail": detail,
            "metrics": metrics,
            "finish_reason": result["finish_reason"],
            "extraction": result["extraction"],
            "normalized_from_fenced": result.get("normalized_from_fenced", False),
            "envelope_bad": envelope_bad,
            "raw_path": str(raw_path.relative_to(ROOT)),
            "output_path": str(output_path.relative_to(ROOT)) if output_path else None,
            "output_abs_path": str(output_path) if output_path else None,
            "content_preview": (result["content"] or "")[:240],
            "tool_calls": result.get("tool_calls") or [],
        }
        attempts.append(attempt)

        if status == "OK" and not envelope_bad:
            break

        if envelope_bad and not envelope_retried:
            envelope_retried = True
            prev = dict(result)
            prev["validation_detail"] = envelope_reason or detail
            pending.insert(0, ("retry_envelope", retry_messages(case, "retry_envelope", prev)))
            continue

        if status != "OK" and metrics.get("syntax_failure") and not syntax_retried:
            syntax_retried = True
            prev = dict(result)
            prev["validation_detail"] = detail
            pending.insert(0, ("retry_syntax", retry_messages(case, "retry_syntax", prev)))
            continue

        if status != "OK" and not validator_retried:
            validator_retried = True
            prev = dict(result)
            prev["validation_detail"] = detail
            pending.insert(0, ("retry_validator", retry_messages(case, "retry_validator", prev)))
            continue

        break

    first = attempts[0]
    selected = choose_best_attempt(attempts)
    selected_output_path = Path(selected["output_abs_path"]) if selected.get("output_abs_path") else None
    final_output_path = promote_case_output(case, selected_output_path)
    if final_output_path:
        selected["output_path"] = str(final_output_path.relative_to(ROOT))

    summary = {
        "case_id": case["id"],
        "group": case["group"],
        "title": case["title"],
        "expected_mode": case["expected_mode"],
        "attempts": attempts,
        "selected_attempt": selected["label"],
        "pass_at_1": first["status"] == "OK" and not first["envelope_bad"],
        "pass_after_one_validator_retry": selected["status"] == "OK" and validator_retried,
        "pass_after_one_envelope_retry": selected["status"] == "OK" and envelope_retried,
        "pass_after_one_syntax_retry": selected["status"] == "OK" and syntax_retried,
        "finish_reason_length_rate": sum(1 for a in attempts if a["finish_reason"] == "length") / len(attempts),
        "final_status": selected["status"],
    }
    merged_metrics: dict[str, Any] = {}
    for attempt in attempts:
        merged_metrics.update({k: v for k, v in attempt["metrics"].items() if v is not None})
    summary["metrics"] = merged_metrics
    return summary

def render_group_report(runtime: dict[str, Any], group_name: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# Damyan Bench {group_name} Report",
        "",
        f"- Benchmark: `{ROOT.name}`",
        f"- Model: `{runtime['model']}`",
        f"- Endpoint: `{BASE}`",
        f"- Mode: `enable_thinking={str(runtime['enable_thinking']).lower()}`",
        f"- Sampling: `temperature={runtime['temperature']}`, `top_p={runtime['top_p']}`, `top_k={runtime['top_k']}`",
        "",
    ]
    counts = Counter(row["final_status"] for row in rows)
    lines.extend(
        [
            "## Summary",
            "",
            f"- Cases: `{len(rows)}`",
            f"- OK: `{counts.get('OK', 0)}`",
            f"- PARTIAL: `{counts.get('PARTIAL', 0)}`",
            f"- NOT-OK: `{counts.get('NOT-OK', 0) + counts.get('SOFT FAIL', 0) + counts.get('HARD FAIL', 0)}`",
            f"- SOFT FAIL: `{counts.get('SOFT FAIL', 0)}`",
            f"- HARD FAIL: `{counts.get('HARD FAIL', 0)}`",
            "",
        ]
    )

    for row in rows:
        lines.extend(
            [
                f"## {row['case_id']} — {row['title']}",
                "",
                f"- Expected mode: `{row['expected_mode']}`",
                f"- Final status: `{row['final_status']}`",
                f"- Pass@1: `{row['pass_at_1']}`",
                f"- Pass after validator retry: `{row['pass_after_one_validator_retry']}`",
                f"- Pass after envelope retry: `{row['pass_after_one_envelope_retry']}`",
                f"- finish_reason=length rate: `{row['finish_reason_length_rate']}`",
                "",
            ]
        )
        if row["metrics"]:
            lines.append("- Metrics:")
            for key, value in row["metrics"].items():
                lines.append(f"  - `{key}`: `{value}`")
            lines.append("")
        lines.append("- Attempts:")
        for attempt in row["attempts"]:
            lines.append(
                f"  - `{attempt['label']}` -> `{attempt['status']}` | finish=`{attempt['finish_reason']}` | extraction=`{attempt['extraction']}`"
            )
            lines.append(f"    - Detail: {attempt['detail']}")
            lines.append(f"    - Raw: `{attempt['raw_path']}`")
            if attempt["output_path"]:
                lines.append(f"    - Output: `{attempt['output_path']}`")
        lines.append("")
    return "\n".join(lines)


def render_summary_report(runtime: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    counts = Counter(row["final_status"] for row in rows)
    group_counts = Counter(row["group"] for row in rows)
    lines = [
        "# Damyan Bench Coding Suite Summary",
        "",
        f"- Benchmark: `{ROOT.name}`",
        f"- Model: `{runtime['model']}`",
        f"- Endpoint: `{BASE}`",
        f"- Mode: `enable_thinking={str(runtime['enable_thinking']).lower()}`",
        f"- Sampling: `temperature={runtime['temperature']}`, `top_p={runtime['top_p']}`, `top_k={runtime['top_k']}`",
        "",
        "## Totals",
        "",
        f"- Cases: `{len(rows)}`",
        f"- OK: `{counts.get('OK', 0)}`",
        f"- PARTIAL: `{counts.get('PARTIAL', 0)}`",
        f"- NOT-OK: `{counts.get('NOT-OK', 0)}`",
        f"- SOFT FAIL: `{counts.get('SOFT FAIL', 0)}`",
        f"- HARD FAIL: `{counts.get('HARD FAIL', 0)}`",
        "",
        "## By Group",
        "",
    ]
    for group in sorted(group_counts):
        subset = [row for row in rows if row["group"] == group]
        group_status = Counter(row["final_status"] for row in subset)
        lines.append(
            f"- `{group}`: cases={len(subset)}, ok={group_status.get('OK', 0)}, partial={group_status.get('PARTIAL', 0)}, non_ok={group_status.get('NOT-OK', 0) + group_status.get('SOFT FAIL', 0) + group_status.get('HARD FAIL', 0)}"
        )
    lines.append("")
    lines.append("## Case Status")
    lines.append("")
    for row in rows:
        lines.append(f"- `{row['case_id']}` `{row['final_status']}` — {row['title']}")
    lines.append("")
    return "\n".join(lines)


def write_reports(runtime: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    ensure_dir(REPORTS_DIR)
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_group.setdefault(row["group"], []).append(row)
    for group, group_rows in by_group.items():
        slug = GROUP_SLUGS[group]
        (REPORTS_DIR / f"{slug}.md").write_text(render_group_report(runtime, group, group_rows))
    (REPORTS_DIR / "summary.md").write_text(render_summary_report(runtime, rows))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Damyan Bench coding suite.")
    parser.add_argument("--base", help="OpenAI-compatible chat completions base URL, e.g. http://127.0.0.1:1234")
    parser.add_argument("--model", help="Model name override. If omitted, use suite.json meta.model.")
    parser.add_argument("--group", action="append", help="Run only the named group. Repeatable.")
    parser.add_argument("--case", action="append", help="Run only the given case id. Repeatable.")
    parser.add_argument("--resume", action="store_true", help="Reuse saved raw responses when present.")
    return parser.parse_args()


def main() -> None:
    global BASE, MODEL
    args = parse_args()
    if args.base:
        BASE = args.base
    if args.model:
        MODEL = args.model
    suite = load_suite()
    runtime = build_runtime_config(suite)
    groups = set(args.group) if args.group else None
    case_ids = set(args.case) if args.case else None
    cases = select_cases(suite, groups, case_ids)
    if not cases:
        raise SystemExit("No matching cases selected.")

    ensure_dir(RAW_DIR)
    ensure_dir(RESP_DIR)
    ensure_dir(ART_DIR)
    ensure_dir(REPORTS_DIR)
    total = len(cases)
    write_status(f"START total_cases={total}")
    results = []
    for idx, case in enumerate(cases, start=1):
        banner = f"[{idx}/{total}] running {case['id']} — {case['title']}"
        print(banner, flush=True)
        write_status(banner)
        result = execute_case(runtime, case, resume=args.resume)
        results.append(result)
        write_status(f"[{idx}/{total}] done {case['id']} status={result['final_status']}")
    write_reports(runtime, results)
    write_status(f"COMPLETE summary={REPORTS_DIR / 'summary.md'}")
    print(REPORTS_DIR / "summary.md")


if __name__ == "__main__":
    main()
