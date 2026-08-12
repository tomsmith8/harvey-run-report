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


def iso_ms(s):
    if not s:
        return None
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)


def normalize_v3(tree):
    """Fold the list-based export shape into the dict-based one.

    v3: {project_id, name, child_name, workflow_id, workflow_state, created_at,
         updated_at, output, steps: [{name, skill, workflow_state, has_output,
         output, inputs, prompt_resolutions}], children: [same, recursive]}

    Differences handled here: steps are a list keyed by 'name' and carry no
    timestamps; children hang off the project with the launching step alias
    encoded as a suffix of child_name; grandchildren are progress-ping noise
    and are dropped.
    """
    root_name = tree.get('name') or ''
    steps = {}
    for s in tree.get('steps') or []:
        steps[s['name']] = {
            'skill': s.get('skill'),
            'status': s.get('workflow_state'),
            'time_start': None, 'time_end': None,
            'inputs': s.get('inputs') or {},
            'outputs': s.get('output') or {},
            'children': [],
        }
    # progress-ping grandchildren are noise, but agent runners can nest at any
    # depth (run_judge_dispute -> child_run_dispute_agent; a fresh doc-ingest
    # launches a swarm runner) - keep every non-ping descendant.
    PING_STEPS = {'agent_progress', 'set_var', 'set_output', 'wait'}
    noise_skipped = 0

    def norm_node(c):
        nonlocal noise_skipped
        node = {
            'pid': c.get('project_id'), 'wid': c.get('workflow_id'),
            'name': c.get('child_name') or c.get('name'), 'status': c.get('workflow_state'),
            'time_start': iso_ms(c.get('created_at')), 'time_end': iso_ms(c.get('updated_at')),
            'steps': {s['name']: {'skill': s.get('skill'), 'status': s.get('workflow_state'),
                                  'inputs': s.get('inputs') or {}, 'outputs': s.get('output') or {}}
                      for s in c.get('steps') or []},
            'children': [],
        }
        for k in c.get('children') or []:
            if {s.get('name') for s in k.get('steps') or []} <= PING_STEPS:
                noise_skipped += 1
                continue
            node['children'].append(norm_node(k))
        return node

    for c in tree.get('children') or []:
        cn = c.get('child_name') or ''
        alias = cn[len(root_name) + 1:] if root_name and cn.startswith(root_name + '_') else None
        if not alias or alias not in steps:
            noise_skipped += 1
            continue
        child = norm_node(c)
        st = steps[alias]
        st['children'].append(child)
        # backfill launch-step timing from its children's project windows
        starts = [t for t in (st['time_start'], child['time_start']) if t]
        ends = [t for t in (st['time_end'], child['time_end']) if t]
        st['time_start'] = min(starts) if starts else None
        st['time_end'] = max(ends) if ends else None
    return {
        'pid': tree.get('project_id'), 'wid': tree.get('workflow_id'),
        'name': root_name, 'status': tree.get('workflow_state'),
        'time_start': iso_ms(tree.get('created_at')), 'time_end': iso_ms(tree.get('updated_at')),
        'steps': steps,
        'stats': {'projects': 1 + len(tree.get('children') or []),
                  'steps': sum(1 for _ in tree.get('steps') or []) +
                           sum(len(c.get('steps') or []) for c in tree.get('children') or []),
                  'noise_children_skipped': noise_skipped},
    }


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
    """Agent transcripts anywhere under a root step's child tree.

    Runners can nest (run_judge_dispute launches child_run_dispute_agent; a
    fresh doc-ingest launches its own swarm runner), so every descendant is
    checked for send_agent_logs / get_agent_logs. A transcript found under a
    doc-ingest child is a per-document ingestion agent (kind='ingest').
    """
    def descendants(node):
        yield node
        for k in node.get('children') or []:
            yield from descendants(k)

    agents = {}
    for alias, step in steps.items():
        for child in step.get('children') or []:
            tsteps = child.get('steps') or {}
            is_ingest = 'parse_document' in tsteps
            fname = ''
            if is_ingest:
                csv = step_result(tsteps.get('set_var', {})) or {}
                furl = csv.get('file_url', '') if isinstance(csv, dict) else ''
                fname = furl.rsplit('/', 1)[-1].split('?')[0] if furl else str(child.get('pid'))
            for node in descendants(child):
                ksteps = node.get('steps') or {}
                sal = ksteps.get('send_agent_logs') or ksteps.get('get_agent_logs')
                if not sal:
                    continue
                params = (sal.get('inputs') or {}).get('request_params') or {}
                msgs = params.get('messages') or []
                if not msgs:
                    continue
                if is_ingest:
                    name, kind = f'ingest: {fname}', 'ingest'
                else:
                    name, kind = re.sub(r'^(run_|write_)', '', alias).replace('_', '-'), 'agent'
                if name in agents:
                    name = f"{name}-{str(node.get('pid'))[-4:]}"
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
                    'project_id': str(node.get('pid', '')),
                    'top_pid': str(child.get('pid', '')),
                    'step': alias,
                    'kind': kind,
                    'start': ts_str(node.get('time_start') or step.get('time_start')),
                    'end': ts_str(node.get('time_end') or step.get('time_end')),
                    'duration_s': ((node.get('time_end') or 0) - (node.get('time_start') or 0)) / 1000 or None,
                    'messages': msgs,
                    'truncated': False,
                    'n_messages': len(msgs),
                    'tools': tool_counts(msgs),
                    'final_answer': final,
                    'agent_label': params.get('agent') or (fname if is_ingest else ''),
                }
                if is_ingest:
                    agents[name]['doc_id'] = re.sub(r'\.\w+$', '', fname)
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
    if isinstance(tree.get('steps'), list):
        print('detected list-based export shape; normalizing')
        tree = normalize_v3(tree)
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
    # both export shapes list steps in pipeline order - keep it
    for alias, step in steps.items():
        dur = None
        if step.get('time_start') and step.get('time_end'):
            dur = (step['time_end'] - step['time_start']) / 1000
        timeline.append({'step': alias, 'start': ts_str(step.get('time_start')),
                         'end': ts_str(step.get('time_end')), 'duration_s': dur})
        inp = step.get('inputs') or {}
        if step.get('skill') == 'IfElseCondition' or 'else_statement' in inp or 'condition_groups' in inp:
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

    agents_meta = [{k: v for k, v in tr.items() if k not in ('messages', 'top_pid')}
                   | {'transcript_truncated': tr['truncated']}
                   for tr in sorted(transcripts.values(), key=lambda t: t['start'] or '')]
    for a, (name, _) in zip(agents_meta, sorted(transcripts.items(), key=lambda kv: kv[1]['start'] or '')):
        a['name'] = name

    # Ingestion children are workers too: no transcript, but a real unit of
    # work per document. Represent them as agents (kind='ingest') so they show
    # in the roster and on the gantt alongside the transcript agents.
    transcript_pids = ({a['project_id'] for a in agents_meta}
                       | {tr.get('top_pid') for tr in transcripts.values() if tr.get('top_pid')})
    for alias, step in steps.items():
        for child in step.get('children') or []:
            ksteps = child.get('steps') or {}
            if 'parse_document' not in ksteps:
                continue
            # a child whose subtree already produced a transcript agent above
            # (itself, or a nested swarm runner) must not be listed twice
            if str(child.get('pid') or '') in transcript_pids:
                continue
            csv = step_result(ksteps.get('set_var', {})) or {}
            csv = csv if isinstance(csv, dict) else {}
            furl = csv.get('file_url', '')
            fname = furl.rsplit('/', 1)[-1].split('?')[0] if furl else str(child.get('pid'))
            strat = step_result(ksteps.get('derive_parse_strategy', {})) or {}
            ref = step_result(ksteps.get('extract_created_ref_id', {})) or {}
            pd_res = step_result(ksteps.get('parse_document', {})) or {}
            els = parse_maybe_json(pd_res.get('elements_json')) if isinstance(pd_res, dict) else None
            n_els = len(els) if isinstance(els, list) else 0
            existed = str(ref.get('already_exists')).lower() == 'true' if isinstance(ref, dict) else False
            outcome = (f"Parsed {fname} with strategy "
                       f"{strat.get('strategy') if isinstance(strat, dict) else '?'} "
                       f"into {n_els} element(s); graph node "
                       f"{(ref.get('ref_id') or '?') if isinstance(ref, dict) else '?'} "
                       + ('already existed (create skipped).' if existed else 'created.'))
            dur = None
            if child.get('time_start') and child.get('time_end'):
                dur = (child['time_end'] - child['time_start']) / 1000
            agents_meta.append({
                'name': f'ingest: {fname}', 'project_id': str(child.get('pid') or ''),
                'step': alias, 'agent_label': fname,
                'start': ts_str(child.get('time_start')), 'end': ts_str(child.get('time_end')),
                'duration_s': dur, 'n_messages': 0, 'tools': {}, 'final_answer': outcome,
                'truncated': False, 'transcript_truncated': False,
                'kind': 'ingest', 'doc_id': re.sub(r'\.\w+$', '', fname),
            })
    agents_meta.sort(key=lambda a: a.get('start') or '')

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
