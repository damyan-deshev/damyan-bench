#!/usr/bin/env python3
import json
from pathlib import Path
import importlib.util

orig = Path(__file__).resolve().parent / 'run_authoring_suite.py'
spec = importlib.util.spec_from_file_location('authoring_suite', orig)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ROOT = Path(__file__).resolve().parent
RAW = ROOT / 'raw'
RESP = ROOT / 'responses'
REPORT = ROOT / 'report.md'


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    RESP.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in mod.CASES:
        raw_path = RAW / f"{case['id']}.json"
        if raw_path.exists():
            out = json.loads(raw_path.read_text())
        else:
            print(f"running {case['id']}", flush=True)
            out = mod.chat(case['prompt'], case['max_tokens'])
            raw_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        msg = out['choices'][0]['message']
        content = msg.get('content') or ''
        code, mode = mod.extract_code(content)
        case_dir = RESP / case['id']
        case_dir.mkdir(parents=True, exist_ok=True)
        code_path = case_dir / case['filename']
        code_path.write_text(code)
        ok, detail = mod.validate(case, code_path)
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
    REPORT.write_text(mod.render_report(rows))
    print(REPORT)

if __name__ == '__main__':
    main()
