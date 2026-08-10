#!/usr/bin/env python3
"""HARVEY run report - single entry point.

  python report.py <input.json|.zip> --out out/            # raw JSON + HTML, no LLM
  python report.py <input.json|.zip> --out out/ --llm      # + failure traces (Claude API)
  python report.py <input.json|.zip> --out out/ --concepts # + concept audit (implies --llm)

Input shape is auto-detected:
  JSON object with "steps"  -> project-tree export (parse_project.py)
  JSON array                -> raw CloudWatch log lines (parse_logs.py, legacy)

Per-run artifacts, always both:
  <out>/report-data.json  raw JSON struct (upload to S3; viewer.html reads it via ?data=<url>)
  <out>/report.html       baked, self-contained HTML view (for hive)

The last stdout line is a JSON manifest for downstream workflows:
  MANIFEST {"report_data": "...", "report_html": "...", "llm": true, "ok": true, "warnings": [...]}
"""
import argparse
import json
import pathlib
import subprocess
import sys
import zipfile

HERE = pathlib.Path(__file__).parent


def detect_parser(path):
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist()
                        if n.lower().endswith('.json') and not n.startswith('__MACOSX'))
            head = zf.read(name)[:200].decode('utf-8', 'ignore').lstrip()
    else:
        head = path.open().read(200).lstrip()
    return 'parse_project.py' if head.startswith('{') else 'parse_logs.py'


def stage(script, *args, required=True, warnings=None):
    cmd = [sys.executable, str(HERE / script), *args]
    print(f'\n== {script} {" ".join(args)}')
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        if required:
            sys.exit(e.returncode)
        msg = f'{script} failed (exit {e.returncode}); report will miss that data'
        print(f'WARN: {msg}')
        if warnings is not None:
            warnings.append(msg)
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help='project-tree JSON or raw log export (.json or .zip)')
    ap.add_argument('--out', default='out')
    ap.add_argument('--llm', action='store_true', help='run Claude analysis (needs ANTHROPIC_API_KEY)')
    ap.add_argument('--concepts', action='store_true', help='also audit concept usage (implies --llm)')
    ap.add_argument('--no-docs', action='store_true', help='skip downloading source documents')
    ap.add_argument('--model', default='claude-opus-5')
    ap.add_argument('--effort', default='high')
    args = ap.parse_args()
    use_llm = args.llm or args.concepts
    warnings = []

    src = pathlib.Path(args.input)
    parser = detect_parser(src)
    print(f'input shape: {"project tree" if parser == "parse_project.py" else "raw log lines (legacy)"}')

    stage(parser, str(src), '--out', args.out)
    if not args.no_docs:
        stage('fetch_docs.py', '--out', args.out, required=False, warnings=warnings)
    if use_llm:
        llm_args = ['--out', args.out, '--model', args.model, '--effort', args.effort]
        if args.concepts:
            llm_args.append('--concepts')
        stage('analyze.py', *llm_args, required=False, warnings=warnings)
    stage('build_report.py', '--out', args.out)

    out = pathlib.Path(args.out)
    manifest = {
        'report_data': str(out / 'report-data.json'),
        'report_html': str(out / 'report.html'),
        'llm': use_llm,
        'ok': not warnings,
        'warnings': warnings,
    }
    print('\nMANIFEST ' + json.dumps(manifest))


if __name__ == '__main__':
    main()
