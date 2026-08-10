#!/usr/bin/env python3
"""Stage 3: assemble the per-run artifacts from the extracted data.

Always writes both:
  <out>/report-data.json  the raw JSON struct (schema_version 1) - upload to S3;
                          also what the hosted viewer.html fetches via ?data=<url>
  <out>/report.html       the baked HTML view - one self-contained file for hive

viewer.html lives in this repo and is hosted once; it is NOT a per-run output.

Usage: python build_report.py --out out/
Missing optional inputs (analysis, docs, workfiles, concepts) degrade gracefully.
"""
import argparse
import json
import pathlib

HERE = pathlib.Path(__file__).parent
SCHEMA_VERSION = 1


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def safe(obj):
    return json.dumps(obj).replace('</', '<\\/')


def build_bundle(out):
    ex = out / 'extracted'
    data = load(out / 'page-data.json', None)
    if data is None:
        raise SystemExit('page-data.json not found - run parse_project.py or parse_logs.py first')
    docs = load(ex / 'source-docs.json', [])
    for d in docs:
        d.pop('plain', None)
    return {
        'schema_version': SCHEMA_VERSION,
        'page_data': data,
        'analysis': load(out / 'analysis.json', {'summaries': [], 'traces': []}),
        'concepts': load(out / 'concepts-analysis.json', {}),
        'source_docs': docs,
        'workfiles': load(ex / 'workfiles.json', []),
        'rubric_links': load(ex / 'rubric-doc-links.json', {}),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='out')
    args = ap.parse_args()
    out = pathlib.Path(args.out)

    bundle = build_bundle(out)
    template = (HERE / 'viewer.html').read_text()

    data_path = out / 'report-data.json'
    data_path.write_text(json.dumps(bundle))
    print(f'wrote {data_path} ({data_path.stat().st_size // 1024} KB)')

    html = (template.replace('__PAGE_DATA__', safe(bundle['page_data']))
                    .replace('__ANALYSIS__', safe(bundle['analysis']))
                    .replace('__SOURCE_DOCS__', safe(bundle['source_docs']))
                    .replace('__WORKFILES__', safe(bundle['workfiles']))
                    .replace('__RUBRIC_LINKS__', safe(bundle['rubric_links']))
                    .replace('__CONCEPTS__', safe(bundle['concepts'])))
    html_path = out / 'report.html'
    html_path.write_text(html)
    print(f'wrote {html_path} ({len(html) // 1024} KB, self-contained)')


if __name__ == '__main__':
    main()
