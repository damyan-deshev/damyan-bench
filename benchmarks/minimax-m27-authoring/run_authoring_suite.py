#!/usr/bin/env python3
import json
import re
import subprocess
import textwrap
from pathlib import Path
from urllib import request

BASE = __import__('os').environ.get('DAMYAN_BENCH_BASE', 'http://127.0.0.1:1234')
MODEL = 'MiniMax-M2.7-UD-IQ3_S'
ROOT = Path(__file__).resolve().parent
RAW = ROOT / 'raw'
RESP = ROOT / 'responses'
ART = ROOT / 'artifacts'
REPORT = ROOT / 'report.md'

CASES = [
    {
        'id': '01-python-slugify',
        'group': 'Code Authoring',
        'title': 'Python utility function',
        'filename': 'slugify.py',
        'max_tokens': 900,
        'prompt': textwrap.dedent('''\
            Write the contents of a single Python file named `slugify.py`.
            Requirements:
            - Define exactly one public function `slugify(title: str) -> str`.
            - Convert to lowercase.
            - Transliterate Unicode to ASCII using only the standard library.
            - Replace any run of non-alphanumeric characters with a single `-`.
            - Trim leading/trailing `-`.
            - If nothing remains, return `"n-a"`.
            - Do not use external packages.
            - Return only the file contents. No markdown fences. No explanation.
        '''),
    },
    {
        'id': '02-python-jsonsum-cli',
        'group': 'Code Authoring',
        'title': 'Python CLI over stdin JSON',
        'filename': 'jsonsum.py',
        'max_tokens': 1100,
        'prompt': textwrap.dedent('''\
            Write the contents of a single Python file named `jsonsum.py`.
            Requirements:
            - CLI usage: `python jsonsum.py FIELD_NAME`.
            - Read a JSON array of objects from stdin.
            - Sum the numeric values for the given field.
            - Print the sum as JSON: `{\"field\": ..., \"sum\": ...}`.
            - If an item is missing the field or the value is not numeric, skip that item.
            - Use only the standard library.
            - Return only the file contents. No markdown fences. No explanation.
        '''),
    },
    {
        'id': '03-python-window-stats',
        'group': 'Code Authoring',
        'title': 'Python mini-module',
        'filename': 'window_stats.py',
        'max_tokens': 1400,
        'prompt': textwrap.dedent('''\
            Write the contents of a single Python file named `window_stats.py`.
            Requirements:
            - Implement three functions:
              1. `parse_csv_numbers(text: str) -> list[float]`
              2. `moving_average(values: list[float], window: int) -> list[float]`
              3. `zscores(values: list[float]) -> list[float]`
            - `parse_csv_numbers` should parse comma-separated numbers, ignoring surrounding whitespace.
            - `moving_average` should return the rolling mean for each full window.
            - `zscores` should return z-scores using population standard deviation; for zero variance return a list of 0.0.
            - Raise `ValueError` for invalid numeric input or invalid window size.
            - Use only the standard library.
            - Return only the file contents. No markdown fences. No explanation.
        '''),
    },
    {
        'id': '04-js-group-by-type',
        'group': 'Code Authoring',
        'title': 'JavaScript data transform',
        'filename': 'groupByType.mjs',
        'max_tokens': 900,
        'prompt': textwrap.dedent('''\
            Write the contents of a single JavaScript module file named `groupByType.mjs`.
            Requirements:
            - Export exactly one named function `groupByType(items)`.
            - Input is an array of objects with keys `type` and `name`.
            - Return an object whose keys are the distinct `type` values.
            - Each value must be an array of the original items for that type, sorted by `name` ascending.
            - Ignore items missing either `type` or `name`.
            - Do not use external packages.
            - Return only the file contents. No markdown fences. No explanation.
        '''),
    },
    {
        'id': '05-js-fetch-timeout',
        'group': 'Code Authoring',
        'title': 'JavaScript async helper',
        'filename': 'fetchJsonWithTimeout.mjs',
        'max_tokens': 1300,
        'prompt': textwrap.dedent('''\
            Write the contents of a single JavaScript module file named `fetchJsonWithTimeout.mjs`.
            Requirements:
            - Export exactly one named async function `fetchJsonWithTimeout(fetchImpl, url, timeoutMs = 1000)`.
            - `fetchImpl` is a fetch-compatible function provided by the caller.
            - Race the fetch against a timeout.
            - If the timeout wins, reject with `new Error("Request timed out")`.
            - If the response is not ok, reject with `new Error("HTTP <status>")`.
            - Otherwise resolve to the parsed JSON body.
            - Do not use external packages.
            - Return only the file contents. No markdown fences. No explanation.
        '''),
    },
    {
        'id': '06-vite-main-js',
        'group': 'Code Authoring',
        'title': 'Vite vanilla JS app entry',
        'filename': 'main.js',
        'max_tokens': 1800,
        'prompt': textwrap.dedent('''\
            Write the contents of a single Vite-compatible browser JavaScript file named `main.js`.
            Requirements:
            - No framework. Plain browser JavaScript only.
            - Assume the page has a `<div id="app"></div>` root.
            - Render a small task explorer UI with:
              - a search input
              - a checkbox labeled `Hide done`
              - a summary line in the form `X shown / Y total`
              - a list of tasks
            - Seed exactly 5 tasks in code, each with `title`, `category`, and `done`.
            - Filter by case-insensitive match on title or category.
            - When `Hide done` is checked, hide completed tasks.
            - Sort visible tasks with incomplete first, then title ascending.
            - Keep the code self-contained in one file and do not import anything.
            - Return only the file contents. No markdown fences. No explanation.
        '''),
    },
]


def chat(prompt: str, max_tokens: int):
    payload = {
        'model': MODEL,
        'stream': False,
        'temperature': 0.2,
        'top_p': 0.95,
        'top_k': 40,
        'max_tokens': max_tokens,
        'chat_template_kwargs': {'enable_thinking': True},
        'messages': [{'role': 'user', 'content': prompt}],
    }
    req = request.Request(
        f'{BASE}/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with request.urlopen(req, timeout=600) as resp:
        body = resp.read().decode('utf-8')
    return json.loads(body)


def extract_code(text: str):
    if text is None:
        return '', 'empty'
    stripped = text.strip()
    m = re.fullmatch(r"```[a-zA-Z0-9_+-]*\n([\s\S]*?)\n```", stripped)
    if m:
        return m.group(1), 'fenced'
    return text, 'raw'


def run(cmd, cwd=None, input_text=None):
    return subprocess.run(cmd, cwd=cwd, input=input_text, text=True, capture_output=True)


def validate(case, code_path):
    cid = case['id']
    if cid == '01-python-slugify':
        cp = run(['python3', '-m', 'py_compile', str(code_path)])
        if cp.returncode != 0:
            return False, f'py_compile failed: {cp.stderr.strip()}'
        test = textwrap.dedent('''\
            import importlib.util
            spec = importlib.util.spec_from_file_location('m', r"%s")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.slugify('Hello, World!') == 'hello-world'
            assert m.slugify('  Café déjà vu  ') == 'cafe-deja-vu'
            assert m.slugify('***') == 'n-a'
            print('OK')
        ''' % code_path)
        cp = run(['python3', '-c', test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip()
    if cid == '02-python-jsonsum-cli':
        cp = run(['python3', '-m', 'py_compile', str(code_path)])
        if cp.returncode != 0:
            return False, f'py_compile failed: {cp.stderr.strip()}'
        sample = '[{"value": 1}, {"value": 2.5}, {"other": 9}, {"value": "x"}]\n'
        cp = run(['python3', str(code_path), 'value'], input_text=sample)
        if cp.returncode != 0:
            return False, (cp.stdout + cp.stderr).strip()
        try:
            parsed = json.loads(cp.stdout)
        except Exception as e:
            return False, f'output is not JSON: {e}; raw={cp.stdout!r}'
        return parsed == {'field': 'value', 'sum': 3.5}, json.dumps(parsed, ensure_ascii=False)
    if cid == '03-python-window-stats':
        cp = run(['python3', '-m', 'py_compile', str(code_path)])
        if cp.returncode != 0:
            return False, f'py_compile failed: {cp.stderr.strip()}'
        test = textwrap.dedent('''\
            import importlib.util, math
            spec = importlib.util.spec_from_file_location('m', r"%s")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            assert m.parse_csv_numbers('1, 2.5,3') == [1.0, 2.5, 3.0]
            assert m.moving_average([1,2,3,4], 2) == [1.5, 2.5, 3.5]
            zs = m.zscores([1,2,3])
            assert len(zs) == 3 and round(zs[0], 3) == -1.225 and round(zs[2], 3) == 1.225
            assert m.zscores([5,5,5]) == [0.0, 0.0, 0.0]
            print('OK')
        ''' % code_path)
        cp = run(['python3', '-c', test])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip()
    if cid == '04-js-group-by-type':
        cp = run(['node', '--check', str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip()
        test = textwrap.dedent('''\
            import { groupByType } from '%s';
            const items = [
              { type: 'b', name: 'zeta' },
              { type: 'a', name: 'beta' },
              { type: 'a', name: 'alpha' },
              { type: 'b', name: 'beta' },
              { nope: true }
            ];
            const out = groupByType(items);
            const expected = {
              a: [{ type: 'a', name: 'alpha' }, { type: 'a', name: 'beta' }],
              b: [{ type: 'b', name: 'beta' }, { type: 'b', name: 'zeta' }]
            };
            if (JSON.stringify(out) !== JSON.stringify(expected)) {
              console.error(JSON.stringify(out));
              process.exit(1);
            }
            console.log('OK');
        ''' % code_path.resolve().as_uri())
        test_path = ART / 'validation' / 'groupByType.test.mjs'
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(test)
        cp = run(['node', str(test_path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip()
    if cid == '05-js-fetch-timeout':
        cp = run(['node', '--check', str(code_path)])
        if cp.returncode != 0:
            return False, cp.stderr.strip()
        test = textwrap.dedent('''\
            import { fetchJsonWithTimeout } from '%s';
            const okFetch = async () => ({ ok: true, json: async () => ({ ok: 1 }) });
            const badFetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
            const slowFetch = () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, json: async () => ({ late: true }) }), 50));
            const a = await fetchJsonWithTimeout(okFetch, 'x', 100);
            if (a.ok !== 1) throw new Error('bad ok path');
            let bad = false;
            try { await fetchJsonWithTimeout(badFetch, 'x', 100); } catch (e) { bad = String(e.message) === 'HTTP 404'; }
            if (!bad) throw new Error('bad status path');
            let timed = false;
            try { await fetchJsonWithTimeout(slowFetch, 'x', 10); } catch (e) { timed = String(e.message) === 'Request timed out'; }
            if (!timed) throw new Error('bad timeout path');
            console.log('OK');
        ''' % code_path.resolve().as_uri())
        test_path = ART / 'validation' / 'fetchJsonWithTimeout.test.mjs'
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(test)
        cp = run(['node', str(test_path)])
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip()
    if cid == '06-vite-main-js':
        harness = ART / 'vite-harness'
        (harness / 'src').mkdir(parents=True, exist_ok=True)
        (harness / 'index.html').write_text('<!doctype html><html><body><div id="app"></div><script type="module" src="/src/main.js"></script></body></html>\n')
        (harness / 'src' / 'main.js').write_text(code_path.read_text())
        cp = run(['npx', 'vite', 'build'], cwd=harness)
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip()
    return False, 'No validator'


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    RESP.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in CASES:
        print(f"running {case['id']}", flush=True)
        out = chat(case['prompt'], case['max_tokens'])
        raw_path = RAW / f"{case['id']}.json"
        raw_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        msg = out['choices'][0]['message']
        content = msg.get('content') or ''
        code, mode = extract_code(content)
        case_dir = RESP / case['id']
        case_dir.mkdir(parents=True, exist_ok=True)
        code_path = case_dir / case['filename']
        code_path.write_text(code)
        ok, detail = validate(case, code_path)
        rows.append({
            'id': case['id'],
            'group': case['group'],
            'title': case['title'],
            'filename': case['filename'],
            'extraction': mode,
            'finish_reason': out['choices'][0].get('finish_reason'),
            'usage': out.get('usage'),
            'status': 'OK' if ok else 'НЕ-ОК',
            'detail': detail,
            'raw_path': str(raw_path.relative_to(ROOT.parent)),
            'code_path': str(code_path.relative_to(ROOT.parent)),
        })
    REPORT.write_text(render_report(rows))
    print(REPORT)


def render_report(rows):
    lines = []
    lines.append('# MiniMax-M2.7 Authoring Report')
    lines.append('')
    lines.append('- Model: `MiniMax-M2.7-UD-IQ3_S`')
    lines.append(f'- Endpoint: `{BASE}`')
    lines.append('- Mode: `enable_thinking=true`')
    lines.append('- Sampling: `temperature=0.2`, `top_p=0.95`, `top_k=40`')
    lines.append('- Phase: `Code Authoring`')
    lines.append('')
    total = len(rows)
    passed = sum(1 for r in rows if r['status'] == 'OK')
    lines.append('## Summary')
    lines.append('')
    lines.append(f'- Total cases: `{total}`')
    lines.append(f'- Passed: `{passed}`')
    lines.append(f'- Failed: `{total - passed}`')
    lines.append('')
    for r in rows:
        lines.append(f"## {r['id']} — {r['title']}")
        lines.append('')
        lines.append(f"- Output file: `{r['code_path']}`")
        lines.append(f"- Raw response: `{r['raw_path']}`")
        lines.append(f"- Finish reason: `{r['finish_reason']}`")
        lines.append(f"- Extraction mode: `{r['extraction']}`")
        lines.append(f"- Status: `{r['status']}`")
        lines.append(f"- Validation: {r['detail']}")
        lines.append('')
    return '\n'.join(lines)


if __name__ == '__main__':
    main()
