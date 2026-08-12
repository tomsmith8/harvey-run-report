#!/usr/bin/env python3
"""Local LLM-flow driver for the stak-scripts prototype.

Bridges the three Script-skill stages to the toolkit's analyze.py so the full
LLM flow (transcript summaries -> failure traces -> concept audit) can be run
locally against a raw export, then merges the results with run-report-build.py
into the final bundle - exactly what the production workflow will do.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  python run_llm_locally.py /path/to/export.json --out /tmp/llm-run [--model claude-opus-5]

Steps:
  1. exec run-report-parse-project.py   (sandbox-style, download_url=file://...)
  2. write the toolkit out-dir shape    (page-data.json, extracted/agents/*.json, ...)
  3. run toolkit analyze.py --concepts  (the real API calls)
  4. exec run-report-build.py           (run_llm=true + the four phase arrays)
  -> <out>/report_data.json             (the bundle Hive renders)
"""
import argparse
import contextlib
import io
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
# repo layout: analyze.py sits in the parent dir; prototype layout: in ../toolkit
TOOLKIT = HERE.parent if (HERE.parent / 'analyze.py').exists() else HERE.parent / 'toolkit'


def run_script(path, injected):
    src = open(path).read()
    g = dict(injected)
    g['__name__'] = 'stakwork_script'
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(src, str(path), 'exec'), g)
    return {k: v for k, v in (l.split(': ', 1) for l in buf.getvalue().splitlines() if ': ' in l)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export_json')
    ap.add_argument('--out', default='/tmp/llm-run')
    ap.add_argument('--model', default='claude-opus-5')
    ap.add_argument('--effort', default='high')
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    (out / 'extracted' / 'agents').mkdir(parents=True, exist_ok=True)

    # 1. parse (sandbox-style)
    url = 'file://' + str(pathlib.Path(args.export_json).resolve())
    o1 = run_script(HERE / 'run-report-parse-project.py', {'download_url': url})
    pd = json.loads(o1['page_data_json'])
    tr = json.loads(o1['transcripts_json'])
    co = json.loads(o1['concepts_json'])
    print(f"parsed: {len(tr)} agents, {pd['score'].get('n_passed')}/{pd['score'].get('n_criteria')}")

    # 2. project into the toolkit's out-dir contract
    json.dump(pd, open(out / 'page-data.json', 'w'))
    for t in tr:
        json.dump({'project_id': t['project_id'], 'step': t['step'],
                   'messages': t['messages'], 'final_answer': t.get('final_answer') or '',
                   'truncated': False},
                  open(out / 'extracted' / 'agents' / f"{t['name']}.json", 'w'))
    json.dump(pd.get('workfiles', []), open(out / 'extracted' / 'workfiles.json', 'w'))
    json.dump([{'id': d['id'], 'file': d.get('file'), 'plain': d.get('plain', '')}
               for d in pd.get('documents', [])],
              open(out / 'extracted' / 'source-docs.json', 'w'))
    json.dump(co.get('by_agent', {}), open(out / 'extracted' / 'concept-pulls.json', 'w'))

    # 3. the real LLM phases via the toolkit
    subprocess.run([sys.executable, str(TOOLKIT / 'analyze.py'), '--out', str(out),
                    '--model', args.model, '--effort', args.effort, '--concepts'], check=True)

    analysis = json.load(open(out / 'analysis.json'))
    concepts = json.load(open(out / 'concepts-analysis.json'))

    # 4. merge exactly as the workflow's build step will
    o4 = run_script(HERE / 'run-report-build.py', {
        'page_data_json': pd, 'run_llm': True,
        'rubric_trace_json': analysis.get('traces', []),
        'transcript_summary_json': analysis.get('summaries', []),
        'concept_audit_json': concepts.get('per_agent', []),
        'concept_synthesis_json': concepts.get('synthesis', {}),
    })
    rd = json.loads(o4['report_data'])
    json.dump(rd, open(out / 'report_data.json', 'w'))
    print(f"mode: {o4['analysis_mode']} | llm: {o4['llm']}")
    print(f"wrote {out / 'report_data.json'}")


if __name__ == '__main__':
    main()
