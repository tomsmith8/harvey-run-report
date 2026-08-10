#!/usr/bin/env python3
"""Stage 2 (API shape): parse the structured project-tree export from the
GET project-data endpoint into the same outputs as parse_logs.py.

Input:  {pid, wid, name, status, time_start, time_end, steps: {alias: step}, stats}
        where step = {skill, status, time_start, time_end, inputs, outputs, children?}
        (times are epoch milliseconds; children are nested project trees).
Output: <out>/extracted/... and <out>/page-data.json  (same contract as parse_logs.py;
        fetch_docs.py / analyze.py / build_report.py run unchanged).

Usage:  python parse_project.py project.json[.zip] --out out/ [--no-redact]
"""
import argparse
import json
import pathlib
import re
import shutil
from datetime import datetime, timezone

from parse_logs import (SECRET_PATTERNS, _FIELD_SECRET, tool_counts,
                        stitch_workfiles, extract_concept_pulls, find_final_answers)


def ts_str(epoch_ms):
    if not epoch_ms:
        return None
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def step_result(step):
    """A step's primary output payload: outputs.output, unwrapping {results: ...}."""
    out = (step.get('outputs') or {}).get('output')
    if isinstance(out, dict) and 'results' in out and isinstance(out['results'], dict):
        return out['results']
    return out


def parse_maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


# ---------------------------------------------------------------- secrets

def collect_secret_values(text):
    values = set()
    for _kind, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            v = m.group(m.lastindex or 0)
            v = re.sub(r'^[^A-Za-z0-9]+', '', v).rstrip('\\"')
            if len(v) >= 16:
                values.add(v)
    for m in _FIELD_SECRET.finditer(text):
        v = m.group(1).rstrip('\\"')
        if len(v) >= 20 and re.search(r'[A-Za-z]', v) and re.search(r'[0-9]', v):
            values.add(v)
    return sorted(values, key=len, reverse=True)


def scan_report(text):
    findings = []
    for kind, pat in SECRET_PATTERNS:
        n = len(pat.findall(text))
        if n:
            findings.append({'kind': kind, 'where': 'project tree', 'count': n,
                             'severity': 'critical' if 'API key' in kind else 'high',
                             'detail': f'A value matching the {kind} pattern appears {n} time(s) '
                                       'in the exported project data. Rotate the credential and mask '
                                       'resolved secrets in step inputs/outputs.'})
    return findings


# ---------------------------------------------------------------- extraction

def find_agent_children(steps):
    """WorkflowRunner steps whose child tree contains an agent transcript."""
    agents = {}
    for alias, step in steps.items():
        for child in step.get('children') or []:
            ksteps = child.get('steps') or {}
            sal = ksteps.get('send_agent_logs') or ksteps.get('get_agent_logs')
            if not sal:
                continue
            params = (sal.get('inputs') or {}).get('request_params') or {}
            msgs = params.get('messages') or []
            if not msgs:
                continue
            name = re.sub(r'^(run_|write_)', '', alias).replace('_', '-')
            if name in agents:
                name = f"{name}-{str(child.get('pid'))[-4:]}"
            final = ''
            fa_out = ((ksteps.get('parse_agent_final_answer') or {}).get('outputs') or {}).get('output')
            if isinstance(fa_out, list):
                strings = [s for s in fa_out if isinstance(s, str)]
                final = max(strings, key=len) if strings else ''
            elif isinstance(fa_out, str):
                final = fa_out
            if not final:
                acc = []
                find_final_answers(ksteps, acc)
                final = max(acc, key=len) if acc else ''
            agents[name] = {
                'project_id': str(child.get('pid', '')),
                'step': alias,
                'start': ts_str(child.get('time_start') or step.get('time_start')),
                'end': ts_str(child.get('time_end') or step.get('time_end')),
                'duration_s': ((child.get('time_end') or 0) - (child.get('time_start') or 0)) / 1000 or None,
                'messages': msgs,
                'truncated': False,
                'n_messages': len(msgs),
                'tools': tool_counts(msgs),
                'final_answer': final,
                'agent_label': params.get('agent', ''),
            }
    return agents


def extract_doc_rows(steps):
    rows = []
    for alias, step in steps.items():
        if alias != 'foreach_ingest_doc':
            continue
        for child in step.get('children') or []:
            ksteps = child.get('steps') or {}
            file_url = ''
            sv = step_result(ksteps.get('set_var', {})) or {}
            if isinstance(sv, dict):
                file_url = sv.get('file_url', '')
            strat = step_result(ksteps.get('derive_parse_strategy', {})) or {}
            ref = step_result(ksteps.get('extract_created_ref_id', {})) or {}
            rows.append({
                'file': file_url.rsplit('/', 1)[-1] if file_url else str(child.get('pid')),
                'project_id': str(child.get('pid', '')),
                'strategy': strat.get('strategy') if isinstance(strat, dict) else None,
                'ref_id': ref.get('ref_id') if isinstance(ref, dict) else None,
                'already_exists': ref.get('already_exists') if isinstance(ref, dict) else None,
                'start': ts_str(child.get('time_start')),
                'end': ts_str(child.get('time_end')),
            })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project', help='project-tree JSON (or .zip containing it)')
    ap.add_argument('--out', default='out')
    ap.add_argument('--no-redact', action='store_true',
                    help='write raw content without masking secret-shaped values (unsafe)')
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    shutil.rmtree(out / 'extracted' / 'agents', ignore_errors=True)
    (out / 'extracted' / 'agents').mkdir(parents=True, exist_ok=True)

    path = pathlib.Path(args.project)
    if path.suffix.lower() == '.zip':
        import zipfile
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if n.lower().endswith('.json') and not n.startswith('__MACOSX')]
            if not names:
                raise SystemExit(f'{path}: no .json file inside the zip')
            raw_text = zf.read(names[0]).decode('utf-8')
    else:
        raw_text = path.read_text()

    security = scan_report(raw_text)
    if not args.no_redact:
        values = collect_secret_values(raw_text)
        n = 0
        for v in values:
            n += raw_text.count(v)
            raw_text = raw_text.replace(v, v[:6] + '***REDACTED***')
        print(f'redacted {n} secret-shaped spans ({len(values)} distinct values)')

    tree = json.loads(raw_text)
    steps = tree.get('steps') or {}
    if not steps:
        raise SystemExit('No steps found - is this the project-tree export shape?')
    print(f"project {tree.get('pid')} ({tree.get('name')}), {len(steps)} steps, status {tree.get('status')}")

    # config from set_var
    sv = step_result(steps.get('set_var', {})) or {}
    if not isinstance(sv, dict):
        sv = {}

    # scores
    scores = {}
    sr = step_result(steps.get('score_rubric', {}))
    if isinstance(sr, dict) and sr.get('scores_json'):
        scores = parse_maybe_json(sr['scores_json']) or {}

    rubrics = parse_maybe_json(sv.get('rubrics_json')) or []
    documents = parse_maybe_json(sv.get('documents_json')) or []
    outputs_map = {}
    ao = step_result(steps.get('assemble_output_map', {}))
    if isinstance(ao, dict):
        outputs_map = parse_maybe_json(ao.get('outputs_json')) or {}

    # timeline + branches
    timeline, branches = [], []
    for alias, step in sorted(steps.items(), key=lambda kv: kv[1].get('time_start') or 0):
        dur = None
        if step.get('time_start') and step.get('time_end'):
            dur = (step['time_end'] - step['time_start']) / 1000
        timeline.append({'step': alias, 'start': ts_str(step.get('time_start')),
                         'end': ts_str(step.get('time_end')), 'duration_s': dur})
        if step.get('skill') == 'IfElseCondition':
            inp = step.get('inputs') or {}
            note = (f"{alias} - then: {inp.get('statement') or '(next step)'}, "
                    f"else: {inp.get('else_statement') or '(next step)'}")
            branches.append(note)
            if inp.get('else_statement') == 'system.succeed':
                branches.append(f'NOTE: {alias} reports success on its else branch - '
                                'a failing run ends as workflow success.')

    transcripts = find_agent_children(steps)
    print(f'agents: {sorted(transcripts)}')
    for name, tr in transcripts.items():
        with open(out / 'extracted' / 'agents' / f'{name}.json', 'w') as f:
            json.dump(tr, f, indent=1)

    workfiles = stitch_workfiles(transcripts)
    concept_pulls = extract_concept_pulls(transcripts)
    doc_rows = extract_doc_rows(steps)

    crit_by_id = {c.get('id'): c for c in scores.get('criteria_results', [])}
    rubric_rows = [{'id': r.get('id'), 'title': r.get('title'),
                    'match_criteria': r.get('match_criteria'),
                    'verdict': crit_by_id.get(r.get('id'), {}).get('verdict', '?'),
                    'reasoning': crit_by_id.get(r.get('id'), {}).get('reasoning', '')}
                   for r in rubrics]

    wall_min = 0
    if tree.get('time_start') and tree.get('time_end'):
        wall_min = round((tree['time_end'] - tree['time_start']) / 60000)

    agents_meta = [{k: v for k, v in tr.items() if k != 'messages'} | {'transcript_truncated': tr['truncated']}
                   for tr in sorted(transcripts.values(), key=lambda t: t['start'] or '')]
    for a, (name, _) in zip(agents_meta, sorted(transcripts.items(), key=lambda kv: kv[1]['start'] or '')):
        a['name'] = name

    stats = tree.get('stats') or {}
    health_notes = ['Transcripts came from the structured project export: no log-line truncation.']
    if stats.get('resolve_errors'):
        health_notes.append(f"{stats['resolve_errors']} resolve errors during export.")
    if stats.get('depth_limit_hit') or stats.get('project_limit_hit'):
        health_notes.append('The export hit a depth or project limit - some children may be missing.')

    payload = {
        'config': {
            'task_slug': sv.get('task_slug'), 'task_goal': sv.get('task_goal'),
            'deliverable': sv.get('task_output_desc'),
            'run_id': tree.get('name'), 'workspace_id': sv.get('workspace_id'),
            'graph_base_url': sv.get('graph_base_url'),
            'models': {k: sv.get(k) for k in
                       ('model', 'checklist_model', 'verify_model', 'cross_check_model', 'judge_model')
                       if sv.get(k)},
            'flags': {k: sv.get(k) for k in
                      ('drafters', 'use_fanout', 'max_iterations', 'use_case_law_research',
                       'cross_checker_agent', 'agent_ingestion_flag') if sv.get(k) is not None},
        },
        'score': {k: scores.get(k) for k in
                  ('score', 'max_score', 'all_pass', 'n_criteria', 'n_passed', 'judge_model', 'scored_at')},
        'rubrics': rubric_rows,
        'timeline': timeline,
        'agents': agents_meta,
        'documents': doc_rows,
        'branches': branches,
        'health_notes': health_notes,
        'wall_clock_min': wall_min,
        'log_stats': {'total_lines': stats.get('steps', len(steps)), 'untagged_lines': 0,
                      'projects': stats.get('projects', 1), 'noise_projects': 0,
                      'transcripts_truncated': 0, 'n_transcripts': len(transcripts)},
        'security': security,
        'outputs': outputs_map,
    }

    ex = out / 'extracted'
    json.dump(scores, open(ex / 'scores.json', 'w'), indent=1)
    json.dump(workfiles, open(ex / 'workfiles.json', 'w'))
    json.dump(concept_pulls, open(ex / 'concept-pulls.json', 'w'), indent=1)
    json.dump(documents, open(ex / 'documents.json', 'w'), indent=1)
    json.dump(payload, open(out / 'page-data.json', 'w'), indent=1)

    print(f"score: {payload['score'].get('n_passed')}/{payload['score'].get('n_criteria')} "
          f"| wall clock {wall_min} min | {len(workfiles)} workfiles | {len(security)} secret finding groups")
    print(f'wrote {out}/page-data.json')


if __name__ == '__main__':
    main()
