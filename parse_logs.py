#!/usr/bin/env python3
"""Stage 2: parse a Stakwork log export, drop noise, extract everything the report needs.

Input:  a JSON array of {"@timestamp": "...", "@message": "..."} objects
        (CloudWatch Logs Insights export for one project tree).
Output: <out>/dump/...           per-workflow/per-project jsonl (noise excluded)
        <out>/extracted/...      structured JSON (scores, timeline, agents, workfiles, ...)
        <out>/page-data.json     everything build_report.py needs

Usage:  python parse_logs.py logs.json --out out/ [--keep-noise]
"""
import argparse
import json
import pathlib
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime

TAG_RE = re.compile(
    r'\[project_id_(\d+)\] \[customer_id_(\d+)\] \[workflow_id_(\d+)\] '
    r'\[workflow_version_id_(\d+)\] \[project_main_parent_id_(\d+)\] '
    r'\[skill_id_(\d+)\] \[step_alias_([^\]]+)\]'
)

# Step aliases that identify a workflow's role, regardless of workflow id.
FINGERPRINTS = [
    ('main', {'score_rubric', 'format_results'}),
    ('agent-runner', {'call_swarm_agent', 'send_agent_logs'}),
    ('doc-ingest', {'parse_document'}),
    ('progress-ping', {'agent_progress', 'wait'}),
]
NOISE_ROLES = {'progress-ping'}


def ts_seconds(ts):
    return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S.%f').timestamp()


def body(msg):
    """Strip the standard tag prefix (and optional [customer-logs-*] marker)."""
    m = TAG_RE.search(msg)
    if not m:
        return msg
    rest = msg[m.end():]
    rest = re.sub(r'^\s*\[customer-logs-[^\]]*\]\s*', '', rest)
    return rest.lstrip()


def classify(aliases):
    for role, needed in FINGERPRINTS:
        if needed <= aliases:
            return role
    return 'other'


def parse_json_after(prefix, text):
    """Parse JSON that follows a known prefix in a log line. Returns None on failure."""
    if not text.startswith(prefix):
        return None
    try:
        return json.loads(text[len(prefix):])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- transcript salvage

def salvage_messages(raw):
    """Recover complete message objects from possibly-truncated JSON with a messages array."""
    idx = raw.find('"messages":[')
    if idx < 0:
        return [], False
    s = raw[idx + len('"messages":['):]
    dec = json.JSONDecoder()
    msgs, pos = [], 0
    while True:
        while pos < len(s) and s[pos] in ' ,\n\t':
            pos += 1
        if pos >= len(s) or s[pos] == ']':
            return msgs, False
        try:
            obj, end = dec.raw_decode(s, pos)
            msgs.append(obj)
            pos = end
        except json.JSONDecodeError:
            return msgs, True  # truncated mid-object


def find_final_answers(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'final_answer' and isinstance(v, str):
                acc.append(v)
            else:
                find_final_answers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            find_final_answers(v, acc)


def tool_counts(messages):
    tools = Counter()
    for m in messages:
        c = m.get('content')
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get('type') == 'tool-call':
                    tools[part.get('toolName', '?')] += 1
    return dict(tools.most_common())


# ---------------------------------------------------------------- workfile stitching

SIMPLE_READ = re.compile(r'^\s*(?:cd [^;&|]+[;&]{1,2}\s*)?(cat|sed -n|grep -n)')


def result_text(part):
    out = part.get('output', part.get('result'))
    if isinstance(out, dict):
        v = out.get('value', out)
        return json.dumps(v) if isinstance(v, dict) else str(v)
    if isinstance(out, list):
        return ' '.join(str(x.get('text', x) if isinstance(x, dict) else x) for x in out)
    return str(out)


def clean_result(res):
    t = res
    if '\\n' in t and t.count('\\n') > t.count('\n'):
        t = t.replace('\\n', '\n').replace('\\"', '"').replace('\\t', '\t')
    cut = t.find('[... output truncated')
    if cut >= 0:
        t = t[:cut]
    return t


def stitch_workfiles(transcripts):
    """Rebuild shared working files from cat/sed/grep reads captured in transcripts.

    Only simple single-read commands are used; sed output is clipped to its line
    range; grep -n line numbers win over positional guesses.
    """
    slots = {}  # fname -> {line_no: (priority, text)}

    def put(f, n, ln, prio):
        slot = slots.setdefault(f, {})
        if n not in slot or prio > slot[n][0]:
            slot[n] = (prio, ln)

    for tr in transcripts.values():
        msgs = tr['messages']
        for i, msg in enumerate(msgs):
            c = msg.get('content')
            if not isinstance(c, list):
                continue
            for part in c:
                if not (isinstance(part, dict) and part.get('type') == 'tool-call'):
                    continue
                inp = part.get('input', part.get('args', {}))
                cmd = inp.get('command', '') if isinstance(inp, dict) else ''
                if not cmd or not SIMPLE_READ.match(cmd):
                    continue
                if i + 1 >= len(msgs) or not isinstance(msgs[i + 1].get('content'), list):
                    continue
                call_id = part.get('toolCallId') or part.get('id')
                res, fallback = '', ''
                for rp in msgs[i + 1]['content']:
                    if isinstance(rp, dict) and rp.get('type') == 'tool-result':
                        rid = rp.get('toolCallId') or rp.get('tool_call_id') or rp.get('id')
                        if call_id and rid == call_id:
                            res = clean_result(result_text(rp))
                            break
                        if not fallback:
                            fallback = clean_result(result_text(rp))
                res = res or fallback
                if not res:
                    continue
                # anchored single-command matchers: a compound command (cat A; cat B)
                # must not be attributed to one file
                prefix = r"^\s*(?:cd [\w./~-]+\s*(?:&&|;)\s*)?"
                gm = re.match(prefix + r"grep -n \"\" ([\w./-]+)(?:\s*\|\s*sed -n '(\d+),(\d+)p')?\s*$", cmd)
                sm = re.match(prefix + r"sed -n '(\d+),(\d+)p' ([\w./-]+)\s*$", cmd)
                cm = re.match(prefix + r"cat ([\w./-]+)(?: 2>/dev/null)?\s*$", cmd)
                if gm:
                    f = short_name(gm.group(1))
                    for lm in re.finditer(r'(?:^|\n)(\d+):(.*)', res):
                        put(f, int(lm.group(1)), lm.group(2), 3)
                elif sm:
                    a, b, f = int(sm.group(1)), int(sm.group(2)), short_name(sm.group(3))
                    for off, ln in enumerate(res.split('\n')[:b - a + 1]):
                        put(f, a + off, ln, 2)
                elif cm:
                    f = short_name(cm.group(1))
                    for off, ln in enumerate(res.split('\n')):
                        put(f, 1 + off, ln, 1)

    out = []
    for f, slot in slots.items():
        if f.startswith('/tmp/') or len(slot) < 3:
            continue
        ns = sorted(slot)
        parts, prev = [], None
        for n in ns:
            if prev is not None and n > prev + 1:
                parts.append(f'\n... [lines {prev + 1}-{n - 1} not recovered - log truncation] ...\n')
            parts.append(slot[n][1])
            prev = n
        text = '\n'.join(parts)
        out.append({
            'id': re.sub(r'[^a-z0-9]+', '_', f.lower()).strip('_'),
            'path': f,
            'title': f,
            'note': f'Reconstructed from agent transcript reads: {len(ns)} lines recovered '
                    f'(lines {ns[0]}-{ns[-1]}). Gaps from the tool-output or log-line limits are marked inline.',
            'text': text,
        })
    return out


def short_name(path):
    """Shorten an absolute artifacts path to its file name; keep relative work/ paths."""
    if '/artifacts/' in path:
        tail = path.split('/artifacts/', 1)[1]
        parts = tail.split('/', 1)
        return parts[1] if len(parts) == 2 else tail
    return path


# ---------------------------------------------------------------- concept pulls

CONCEPT_TYPES = ('Concept', 'Doctrine', 'LegalArgument')


def extract_concept_pulls(transcripts):
    pulls = {}
    for name, tr in transcripts.items():
        queries, nodes = [], {}
        for i, msg in enumerate(tr['messages']):
            c = msg.get('content')
            if not isinstance(c, list):
                continue
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'tool-call':
                    inp = part.get('input', part.get('args', {}))
                    if not isinstance(inp, dict):
                        continue
                    typ = str(inp.get('type', inp.get('node_type', '')))
                    if any(t in typ for t in CONCEPT_TYPES):
                        queries.append({'msg': i, 'tool': part.get('toolName', ''),
                                        'q': str(inp.get('q', inp.get('ref_id', '')))[:200],
                                        'type': typ, 'namespace': inp.get('namespace', '(none)')})
                elif part.get('type') == 'tool-result':
                    blob = json.dumps(part.get('output', ''))
                    for m in re.finditer(
                            r'name\\?":\\?"([^"\\\\]+)\\?",\\?"node_type\\?":\\?"(Concept|Doctrine|LegalArgument)', blob):
                        nm, nt = m.groups()
                        if not any(v['name'] == nm for v in nodes.values()):
                            nodes['byname:' + nm] = {'name': nm, 'node_type': nt, 'first_msg': i}
                    for m in re.finditer(
                            r'node_type\\?":\\?"(Concept|Doctrine|LegalArgument)\\?",.{0,200}?name\\?":\\?"([^"\\\\]+)', blob):
                        nt, nm = m.groups()
                        if not any(v['name'] == nm for v in nodes.values()):
                            nodes['byname:' + nm] = {'name': nm, 'node_type': nt, 'first_msg': i}
        pulls[name] = {'queries': queries, 'nodes': list(nodes.values())}
    return pulls


# ---------------------------------------------------------------- secrets scan (redacting)

# The ampersand in these logs often survives as the literal text "&" or "&amp;".
_AMP = r'(?:[?&]|\\u0026|&amp;)'
SECRET_PATTERNS = [
    ('Anthropic API key', re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}')),
    ('OpenAI-style API key', re.compile(r'\bsk-(?!ant-)[A-Za-z0-9_-]{30,}')),
    ('LlamaParse API key', re.compile(r'llx-[A-Za-z0-9]{16,}')),
    ('JWT', re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('Query token', re.compile(_AMP + r'(?:run_)?(?:token|api_?key)=[A-Fa-f0-9]{32,}')),
    ('Service token in env dump', re.compile(r'\b[A-Z][A-Z0-9_]*TOKEN=(?!\{\{)[A-Za-z0-9_-]{16,}')),
    ('Static service token in header', re.compile(r'x-api-token\\?":\s*\\?"(?!\{\{)[A-Za-z0-9_-]{16,}')),
]


def scan_secrets(events):
    """Report where secret-looking values appear in log lines. Never emits the values."""
    findings = {}
    for ev in events:
        msg = ev['@message']
        for kind, pat in SECRET_PATTERNS:
            if pat.search(msg):
                m = TAG_RE.search(msg)
                where = f'step {m.group(7)}' if m else 'untagged line'
                key = (kind, where)
                findings[key] = findings.get(key, 0) + 1
    return [
        {'kind': kind, 'where': where, 'count': n,
         'severity': 'critical' if 'API key' in kind else 'high',
         'detail': f'A value matching the {kind} pattern appears in plain text in {n} log line(s) at {where}. '
                   'Rotate the credential and mask resolved secrets before logging.'}
        for (kind, where), n in sorted(findings.items(), key=lambda kv: -kv[1])
    ]


# Field-form collector: catches secrets assigned to credential-named JSON fields
# or env vars at any position ("api_key": "...", API_TOKEN=..., "secret":"...").
_FIELD_SECRET = re.compile(
    r'(?:api_?key|secret|password|access_token|auth_token|token)'
    r'\\*"?\s*[:=]\s*\\*"?(?!\{\{)([A-Za-z0-9_.\-]{20,})', re.I)


def _secret_values(events):
    """Collect the actual secret values so every escape-depth variant can be erased."""
    values = set()
    for ev in events:
        msg = ev['@message']
        for _kind, pat in SECRET_PATTERNS:
            for m in pat.finditer(msg):
                v = m.group(m.lastindex or 0)
                v = re.sub(r'^[^A-Za-z0-9]+', '', v)
                v = v.rstrip('\\"')
                if len(v) >= 16:
                    values.add(v)
        for m in _FIELD_SECRET.finditer(msg):
            v = m.group(1).rstrip('\\"')
            # require mixed letters+digits so plain words and slugs are not nuked
            if len(v) >= 20 and re.search(r'[A-Za-z]', v) and re.search(r'[0-9]', v):
                values.add(v)
    return sorted(values, key=len, reverse=True)


def redact_events(events):
    """Erase every occurrence of every collected secret value, at any escape depth.

    This is the single choke point: dumps, transcripts, workfiles, and page-data
    are all derived from these messages afterwards.
    """
    values = _secret_values(events)
    n = 0
    for ev in events:
        msg = ev['@message']
        for v in values:
            if v in msg:
                n += msg.count(v)
                msg = msg.replace(v, v[:6] + '***REDACTED***')
        ev['@message'] = msg
    return n


# ---------------------------------------------------------------- main pipeline extraction

def extract_main(lines):
    """Pull config, timeline, scores, and branch decisions from the main workflow's log lines."""
    out = {'set_var': None, 'scores': None, 'rubrics': None, 'documents': None,
           'timeline': [], 'branches': [], 'launch_windows': []}
    # A step alias can execute more than once (retry loops): keep one execution
    # record per Starting/Finishing pair, in order.
    execs = defaultdict(list)  # alias -> [{'start':ts,'end':ts}]
    order = []
    for l in lines:
        b = body(l['msg'])
        s = l['step']
        if b == f'Starting {s}':
            order.append((s, len(execs[s])))
            execs[s].append({'start': l['ts'], 'end': None})
        elif b.startswith('Finishing step'):
            open_execs = [e for e in execs[s] if e['end'] is None]
            if open_execs:
                open_execs[-1]['end'] = l['ts']
            else:
                execs[s].append({'start': None, 'end': l['ts']})

        attrs = parse_json_after('Step attributes: ', b)
        if attrs is not None:
            if s == 'set_var' and out['set_var'] is None and 'vars' in attrs:
                out['set_var'] = attrs
            if s.startswith(('guard_', 'if_')):
                out['branches'].append({
                    'step': s,
                    'statement': attrs.get('statement'),
                    'else_statement': attrs.get('else_statement'),
                })
            wf_id = attrs.get('workflow_id')
            if wf_id:
                # attach this launch to the execution of s that is open at this line
                idx = max(0, len([e for e in execs[s] if e['start']]) - 1) if execs[s] else 0
                out['launch_windows'].append({'step': s, 'workflow_id': str(wf_id), 'exec_idx': idx})
        got = parse_json_after('Got data: ', b)
        if got is not None and s == 'score_rubric':
            raw = got.get('results', {}).get('scores_json')
            if raw:
                try:
                    out['scores'] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        if got is not None and s == 'assemble_output_map':
            raw = got.get('results', {}).get('outputs_json')
            if raw:
                try:
                    out['outputs'] = json.loads(raw)
                except json.JSONDecodeError:
                    pass

    for s, idx in order:
        e = execs[s][idx]
        dur = None
        if e['start'] and e['end']:
            dur = ts_seconds(e['end']) - ts_seconds(e['start'])
        label = s if idx == 0 else f'{s} #{idx + 1}'
        out['timeline'].append({'step': label, 'start': e['start'], 'end': e['end'], 'duration_s': dur})
    # attach each launch record to its own execution's window
    for lw in out['launch_windows']:
        es = execs.get(lw['step'], [])
        e = es[min(lw.get('exec_idx', 0), len(es) - 1)] if es else {}
        lw['start'], lw['end'] = e.get('start'), e.get('end')

    sv = (out['set_var'] or {}).get('vars', {})
    for key, target in (('rubrics_json', 'rubrics'), ('documents_json', 'documents')):
        v = sv.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = None
        out[target] = v
    return out


def extract_doc_ingest(projects):
    rows = []
    for pid, lines in sorted(projects.items()):
        rec = {'project_id': pid, 'start': lines[0]['ts'], 'end': lines[-1]['ts'],
               'file': None, 'strategy': None, 'ref_id': None, 'already_exists': None}
        for l in lines:
            b = body(l['msg'])
            attrs = parse_json_after('Step attributes: ', b)
            if attrs and 'vars' in attrs and rec['file'] is None:
                url = attrs['vars'].get('file_url', '')
                if url:
                    rec['file'] = url.rsplit('/', 1)[-1]
            got = parse_json_after('Got data: ', b)
            if got:
                res = got.get('results', {})
                rec['strategy'] = res.get('strategy', rec['strategy'])
                if 'ref_id' in res:
                    rec['ref_id'] = res['ref_id']
                    rec['already_exists'] = res.get('already_exists')
        rows.append(rec)
    return rows


def extract_transcripts(agent_projects, launch_windows):
    """Map agent-runner child projects to the parent steps that launched them, then salvage."""
    windows = [lw for lw in launch_windows if lw.get('start') and lw.get('end')]
    transcripts = {}
    for pid, lines in agent_projects.items():
        first, last = ts_seconds(lines[0]['ts']), ts_seconds(lines[-1]['ts'])
        # Parallel launch steps have overlapping windows: pick the tightest one
        # that contains this child (least slack on both sides).
        step, best_slack = None, None
        for lw in windows:
            ws, we = ts_seconds(lw['start']), ts_seconds(lw['end'])
            if ws - 5 <= first and last <= we + 5:
                slack = abs(first - ws) + abs(we - last)
                if best_slack is None or slack < best_slack:
                    step, best_slack = lw['step'], slack
        name = re.sub(r'^(run_|write_)', '', step) if step else f'agent-{pid}'
        name = name.replace('_', '-')
        if name in transcripts:  # a fan-out step launched several children
            name = f'{name}-{pid[-4:]}'

        best_msgs, truncated = [], False
        final_answer = ''
        for l in lines:
            b = body(l['msg'])
            raw = None
            if l['step'] == 'send_agent_logs' and b.startswith('Step attributes: '):
                raw = b[len('Step attributes: '):]
            elif l['step'] == 'get_agent_logs' and b.startswith('{"sessionId"'):
                raw = b
            if raw:
                msgs, trunc = salvage_messages(raw)
                if len(msgs) > len(best_msgs):
                    best_msgs, truncated = msgs, trunc
            if l['step'] == 'parse_agent_final_answer' and b.startswith('Step attributes: '):
                try:
                    attrs = json.loads(b[len('Step attributes: '):])
                    acc = []
                    find_final_answers(attrs, acc)
                    if acc:
                        final_answer = max(acc, key=len)
                except json.JSONDecodeError:
                    pass

        if not best_msgs and not final_answer:
            continue
        transcripts[name] = {
            'project_id': pid, 'step': step or '(unmapped)',
            'start': lines[0]['ts'], 'end': lines[-1]['ts'],
            'duration_s': last - first,
            'messages': best_msgs, 'truncated': truncated,
            'n_messages': len(best_msgs),
            'tools': tool_counts(best_msgs),
            'final_answer': final_answer,
        }
    return transcripts


# ---------------------------------------------------------------- entry point

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('logs', help='CloudWatch export: JSON array of {@timestamp, @message}')
    ap.add_argument('--out', default='out', help='output directory')
    ap.add_argument('--keep-noise', action='store_true', help='also dump progress-ping projects')
    ap.add_argument('--no-redact', action='store_true',
                    help='write raw log content without masking secret-shaped values (unsafe)')
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    # clear stale outputs so a re-run cannot mix old and new agents/dumps
    shutil.rmtree(out / 'dump', ignore_errors=True)
    shutil.rmtree(out / 'extracted' / 'agents', ignore_errors=True)
    (out / 'dump').mkdir(parents=True, exist_ok=True)
    (out / 'extracted' / 'agents').mkdir(parents=True, exist_ok=True)

    logs_path = pathlib.Path(args.logs)
    if logs_path.suffix.lower() == '.zip':
        import zipfile
        with zipfile.ZipFile(logs_path) as zf:
            names = [n for n in zf.namelist()
                     if n.lower().endswith('.json') and not n.startswith('__MACOSX')]
            if not names:
                raise SystemExit(f'{logs_path}: no .json file inside the zip')
            events = json.loads(zf.read(names[0]).decode('utf-8'))
            print(f'read {names[0]} from zip')
    else:
        events = json.loads(logs_path.read_text())
    print(f'{len(events)} log events')

    security = scan_secrets(events)  # scan BEFORE redaction so the report sees originals
    if not args.no_redact:
        masked = redact_events(events)
        print(f'redacted {masked} secret-shaped spans')

    # group by (workflow, project)
    by_wf_proj = defaultdict(list)
    wf_aliases = defaultdict(set)
    untagged = 0
    for ev in events:
        m = TAG_RE.search(ev['@message'])
        if not m:
            untagged += 1
            continue
        pid, _cid, wfid, _v, _parent, _skill, alias = m.groups()
        by_wf_proj[(wfid, pid)].append({'ts': ev['@timestamp'], 'step': alias, 'msg': ev['@message']})
        wf_aliases[wfid].add(alias)

    roles = {wfid: classify(aliases) for wfid, aliases in wf_aliases.items()}
    role_counts = Counter(roles.values())
    print('workflow roles:', dict(role_counts))
    if 'main' not in roles.values():
        raise SystemExit('No main workflow found (needs steps score_rubric + format_results). '
                         'Check the export covers the full project tree.')

    projects_by_role = defaultdict(dict)
    noise_projects, noise_lines = 0, 0
    for (wfid, pid), lines in by_wf_proj.items():
        lines.sort(key=lambda x: x['ts'])
        role = roles[wfid]
        if role in NOISE_ROLES:
            noise_projects += 1
            noise_lines += len(lines)
            if not args.keep_noise:
                continue
        projects_by_role[role][pid] = lines
        d = out / 'dump' / f'{role}-{wfid}'
        d.mkdir(exist_ok=True)
        with open(d / f'project-{pid}.jsonl', 'w') as f:
            for l in lines:
                f.write(json.dumps(l) + '\n')

    # main workflow = the main-role project with the most lines
    main_projects = projects_by_role['main']
    main_pid, main_lines = max(main_projects.items(), key=lambda kv: len(kv[1]))
    print(f'main project: {main_pid} ({len(main_lines)} lines)')
    main_ext = extract_main(main_lines)

    transcripts = extract_transcripts(projects_by_role.get('agent-runner', {}), main_ext['launch_windows'])
    print(f'agents: {sorted(transcripts)}')
    for name, tr in transcripts.items():
        with open(out / 'extracted' / 'agents' / f'{name}.json', 'w') as f:
            json.dump(tr, f, indent=1)

    workfiles = stitch_workfiles(transcripts)
    concept_pulls = extract_concept_pulls(transcripts)
    doc_rows = extract_doc_ingest(projects_by_role.get('doc-ingest', {}))

    scores = main_ext['scores'] or {}
    crit_by_id = {c.get('id'): c for c in scores.get('criteria_results', [])}
    rubric_rows = []
    for r in main_ext['rubrics'] or []:
        c = crit_by_id.get(r.get('id'), {})
        rubric_rows.append({'id': r.get('id'), 'title': r.get('title'),
                            'match_criteria': r.get('match_criteria'),
                            'verdict': c.get('verdict', '?'), 'reasoning': c.get('reasoning', '')})

    sv = (main_ext['set_var'] or {}).get('vars', {})
    tl = [t for t in main_ext['timeline'] if t['start'] and t['end']]
    wall_min = round((ts_seconds(tl[-1]['end']) - ts_seconds(tl[0]['start'])) / 60) if tl else 0

    agents_meta = [{
        'name': n, 'step': tr['step'], 'project_id': tr['project_id'],
        'start': tr['start'], 'end': tr['end'], 'duration_s': tr['duration_s'],
        'n_messages': tr['n_messages'], 'transcript_truncated': tr['truncated'],
        'tools': tr['tools'], 'final_answer': tr['final_answer'], 'agent_label': '',
    } for n, tr in sorted(transcripts.items(), key=lambda kv: kv[1]['start'])]

    branch_notes = []
    for br in main_ext['branches']:
        note = f"{br['step']} - then: {br['statement'] or '(next step)'}, else: {br['else_statement'] or '(next step)'}"
        branch_notes.append(note)
        if br['else_statement'] == 'system.succeed':
            branch_notes.append(
                f"NOTE: {br['step']} reports success on its else branch - a failing run ends as workflow success.")

    n_trunc = sum(1 for t in transcripts.values() if t['truncated'])
    health_notes = [
        f'{n_trunc} of {len(transcripts)} agent transcripts were cut by the log-line size limit. '
        'Store transcripts as artifacts and log the pointer.',
        f'{noise_projects} progress-ping projects ({noise_lines} lines, '
        f'{round(100 * noise_lines / max(1, len(events)))}% of log volume) carry no analytical value.',
    ]

    payload = {
        'config': {
            'task_slug': sv.get('task_slug'), 'task_goal': sv.get('task_goal'),
            'deliverable': sv.get('task_output_desc'),
            'run_id': None, 'workspace_id': sv.get('workspace_id'),
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
        'timeline': main_ext['timeline'],
        'agents': agents_meta,
        'documents': doc_rows,
        'branches': branch_notes,
        'health_notes': health_notes,
        'wall_clock_min': wall_min,
        'log_stats': {'total_lines': len(events), 'untagged_lines': untagged,
                      'projects': len(by_wf_proj), 'noise_projects': noise_projects,
                      'transcripts_truncated': n_trunc, 'n_transcripts': len(transcripts)},
        'security': security,
        'outputs': main_ext.get('outputs', {}),
    }

    ex = out / 'extracted'
    json.dump(scores, open(ex / 'scores.json', 'w'), indent=1)
    json.dump(workfiles, open(ex / 'workfiles.json', 'w'))
    json.dump(concept_pulls, open(ex / 'concept-pulls.json', 'w'), indent=1)
    json.dump(main_ext['documents'] or [], open(ex / 'documents.json', 'w'), indent=1)
    json.dump(payload, open(out / 'page-data.json', 'w'), indent=1)

    print(f"score: {payload['score'].get('n_passed')}/{payload['score'].get('n_criteria')} "
          f"| wall clock {wall_min} min | {len(workfiles)} workfiles recovered "
          f"| {len(security)} secret finding groups")
    print(f'wrote {out}/page-data.json')


if __name__ == '__main__':
    main()
