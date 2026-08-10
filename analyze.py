#!/usr/bin/env python3
"""Stage 4: LLM analysis. Reads the extracted data, runs the prompt templates in
prompts/ through the Claude API with structured outputs, and writes
analysis.json (+ concepts-analysis.json with --concepts).

Usage:
  export ANTHROPIC_API_KEY=...   (or `ant auth login`)
  python analyze.py --out out/ [--model claude-opus-5] [--effort high]
                    [--concepts] [--workers 4] [--no-fallback]

The prompt files are runner-agnostic: each is a template with {PLACEHOLDERS}.
This runner fills them and forces a JSON schema on the response; the same
templates can be dropped into a Stakwork workflow instead.
"""
import argparse
import concurrent.futures as cf
import json
import pathlib
import re
import sys

import anthropic

HERE = pathlib.Path(__file__).parent
MAX_TRANSCRIPT_CHARS = 500_000
MAX_TOKENS = 32_000

# ------------------------------------------------------------------ schemas

def strictify(schema):
    """Recursively prepare a schema for structured outputs."""
    if isinstance(schema, dict):
        if schema.get('type') == 'object' and 'properties' in schema:
            schema.setdefault('additionalProperties', False)
            schema.setdefault('required', list(schema['properties'].keys()))
        for v in schema.values():
            strictify(v)
    elif isinstance(schema, list):
        for v in schema:
            strictify(v)
    return schema


SUMMARY_SCHEMA = strictify({
    'type': 'object',
    'properties': {
        'agent_name': {'type': 'string'},
        'mission': {'type': 'string'},
        'tools': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'count': {'type': 'integer'}, 'purpose': {'type': 'string'}}}},
        'files_touched': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'action': {'type': 'string', 'enum': ['read', 'write', 'read+write', 'checked']},
            'note': {'type': 'string'}}}},
        'context_gathered': {'type': 'string'},
        'key_findings': {'type': 'array', 'items': {'type': 'string'}},
        'anomalies': {'type': 'array', 'items': {'type': 'string'}},
        'failed_rubric_relevance': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'rubric_id': {'type': 'string'}, 'note': {'type': 'string'}}}},
    },
})

TRACE_SCHEMA = strictify({
    'type': 'object',
    'properties': {
        'rubric_id': {'type': 'string'},
        'pathway': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'station': {'type': 'string'},
            'status': {'type': 'string', 'enum': ['present', 'partial', 'diverged', 'missing', 'not-applicable']},
            'evidence': {'type': 'string'}}}},
        'q_ingested_to_graph': {'type': 'object', 'properties': {
            'answer': {'type': 'string', 'enum': ['yes', 'no', 'partial']}, 'evidence': {'type': 'string'}}},
        'q_knowable_or_derived': {'type': 'object', 'properties': {
            'answer': {'type': 'string',
                       'enum': ['knowable-explicit', 'derived', 'partially-knowable', 'not-knowable']},
            'evidence': {'type': 'string'}}},
        'q_draft_got_it': {'type': 'object', 'properties': {
            'answer': {'type': 'string', 'enum': ['yes', 'no', 'partial', 'diverged-deliberately']},
            'evidence': {'type': 'string'}}},
        'q_verify_got_it': {'type': 'object', 'properties': {
            'answer': {'type': 'string', 'enum': ['yes', 'no', 'partial', 'flagged-but-overridden']},
            'evidence': {'type': 'string'}}},
        'root_cause': {'type': 'string'},
        'classification': {'type': 'string', 'enum': [
            'agent-miss', 'deliberate-divergence-vs-rubric', 'rubric-flaw',
            'judge-strictness', 'data-gap', 'ingestion-gap']},
        'fix_suggestions': {'type': 'array', 'items': {'type': 'string'}},
    },
})

CONCEPT_AUDIT_SCHEMA = strictify({
    'type': 'object',
    'properties': {
        'agent_name': {'type': 'string'},
        'pulled': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'node_type': {'type': 'string'}, 'how': {'type': 'string'},
            'content_gist': {'type': 'string'}, 'applied': {'type': 'string'},
            'verdict': {'type': 'string', 'enum': ['effective', 'partially-used', 'ignored', 'irrelevant-noise']},
            'evidence': {'type': 'string'}}}},
        'missed_opportunities': {'type': 'array', 'items': {'type': 'string'}},
        'overall_assessment': {'type': 'string'},
        'counts': {'type': 'object', 'properties': {
            'pulled': {'type': 'integer'}, 'effective': {'type': 'integer'},
            'ignored': {'type': 'integer'}, 'noise': {'type': 'integer'}}},
    },
})

CONCEPT_SYNTH_SCHEMA = strictify({
    'type': 'object',
    'properties': {
        'overall_narrative': {'type': 'string'},
        'concept_matrix': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'concept': {'type': 'string'}, 'agents': {'type': 'array', 'items': {'type': 'string'}},
            'verdict': {'type': 'string', 'enum': ['effective', 'partially-used', 'ignored', 'irrelevant-noise']},
            'note': {'type': 'string'}}}},
        'relation_to_failures': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'rubric_id': {'type': 'string'}, 'finding': {'type': 'string'}}}},
        'recommendations': {'type': 'array', 'items': {'type': 'string'}},
    },
})

# ------------------------------------------------------------------ API call

def call_claude(client, args, prompt, schema, label):
    kwargs = dict(
        model=args.model,
        max_tokens=MAX_TOKENS,
        output_config={'format': {'type': 'json_schema', 'schema': schema},
                       'effort': args.effort},
        messages=[{'role': 'user', 'content': prompt}],
    )
    if not args.no_fallback:
        kwargs['betas'] = ['server-side-fallback-2026-07-01']
        kwargs['fallbacks'] = 'default'
    api = client.beta.messages if not args.no_fallback else client.messages

    last_err = None
    for attempt in range(2):
        try:
            with api.stream(**kwargs) as stream:
                msg = stream.get_final_message()
            if msg.stop_reason == 'refusal':
                raise RuntimeError(f'{label}: request was refused by safety classifiers')
            if msg.stop_reason == 'max_tokens':
                raise RuntimeError(f'{label}: output truncated at {MAX_TOKENS} tokens '
                                   '(thinking + JSON exceeded max_tokens) - raise MAX_TOKENS')
            text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text')
            return json.loads(text)
        # retry only transient failures; deterministic 4xx errors propagate immediately
        except (json.JSONDecodeError, anthropic.RateLimitError,
                anthropic.InternalServerError, anthropic.APIConnectionError) as e:
            last_err = e
            print(f'  retry {label}: {type(e).__name__}: {e}', file=sys.stderr)
    raise RuntimeError(f'{label}: failed after retries: {last_err}')


# ------------------------------------------------------------------ context builders

def load_prompt(name):
    return (HERE / 'prompts' / f'{name}.md').read_text()


def fill(template, **kw):
    out = template
    for k, v in kw.items():
        out = out.replace('{' + k + '}', v if isinstance(v, str) else json.dumps(v, indent=1))
    return out


def run_context(page):
    failed = [r for r in page['rubrics'] if r['verdict'] == 'fail']
    lines = [
        f"Task: {page['config'].get('task_goal')}",
        f"Deliverable: {page['config'].get('deliverable')}",
        f"Models: {json.dumps(page['config'].get('models'))}",
        f"Score: {page['score'].get('n_passed')}/{page['score'].get('n_criteria')} criteria passed "
        f"(judge {page['score'].get('judge_model')}).",
        'Failed criteria:',
    ]
    for r in failed:
        lines.append(f"- {r['id']} {r['title']} | match: {r['match_criteria']} | judge: {r['reasoning']}")
    return '\n'.join(lines)


def grep_excerpts(text, terms, context=1, max_lines=60):
    lines = text.split('\n')
    keep, out = set(), []
    lowered = [l.lower() for l in lines]
    for t in terms:
        tl = t.lower()
        for i, l in enumerate(lowered):
            if tl in l:
                keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    prev = None
    for i in sorted(keep):
        if prev is not None and i > prev + 1:
            out.append('...')
        out.append(lines[i])
        prev = i
        if len(out) >= max_lines:
            break
    return '\n'.join(out)


def run_parallel(fn, items, workers, phase):
    """Run fn over items in parallel; a failed item is reported and skipped, not fatal."""
    results = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for fut in cf.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                print(f'  {phase}: item failed and was skipped: {e}', file=sys.stderr)
    return results


def rubric_terms(rubric):
    txt = (rubric.get('title') or '') + ' ' + (rubric.get('match_criteria') or '')
    toks = set(re.findall(r'\$?\d[\d,]*\.?\d*', txt))
    toks.update(w for w in re.findall(r'[A-Za-z][A-Za-z-]{5,}', txt)
                if w.lower() not in {'criteria', 'memoranda', 'memorandum', 'includes', 'explains',
                                     'identifies', 'calculates', 'material', 'materially'})
    return [t for t in toks if len(t) > 2][:20]


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='out')
    ap.add_argument('--model', default='claude-opus-5')
    ap.add_argument('--effort', default='high', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    ap.add_argument('--concepts', action='store_true', help='also run the concept-usage audit')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--no-fallback', action='store_true',
                    help='disable the server-side refusal fallback (non-beta endpoint)')
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    ex = out / 'extracted'
    page = json.loads((out / 'page-data.json').read_text())
    ctx = run_context(page)
    client = anthropic.Anthropic()

    agents = {}
    for p in sorted((ex / 'agents').glob('*.json')):
        agents[p.stem] = json.loads(p.read_text())

    # ---- phase A: transcript summaries (parallel) ----
    tmpl = load_prompt('transcript_summary')

    def summarize(item):
        name, tr = item
        prompt = fill(tmpl, AGENT_NAME=name, AGENT_STEP=tr.get('step', ''), RUN_CONTEXT=ctx,
                      TRANSCRIPT_JSON=json.dumps(tr['messages'])[:MAX_TRANSCRIPT_CHARS],
                      FINAL_ANSWER=tr.get('final_answer', ''))
        return call_claude(client, args, prompt, SUMMARY_SCHEMA, f'summary:{name}')

    summaries = run_parallel(summarize, list(agents.items()), args.workers, 'summaries')
    json.dump({'summaries': summaries, 'traces': []}, open(out / 'analysis.json', 'w'), indent=1)
    print(f'summaries: {len(summaries)}/{len(agents)} (persisted)')

    # ---- phase B: failed-rubric traces (parallel) ----
    failed = [r for r in page['rubrics'] if r['verdict'] == 'fail']
    stations = ['source-docs', 'graph-ingest'] + [a['name'] for a in page['agents']] + ['judge']
    workfiles = json.loads((ex / 'workfiles.json').read_text()) if (ex / 'workfiles.json').exists() else []
    src_docs = json.loads((ex / 'source-docs.json').read_text()) if (ex / 'source-docs.json').exists() else []
    trace_tmpl = load_prompt('rubric_trace')

    def trace(rubric):
        terms = rubric_terms(rubric)
        doc_ex = '\n\n'.join(f"### {d['file']}\n{grep_excerpts(d['plain'], terms)}"
                             for d in src_docs if grep_excerpts(d['plain'], terms))
        wf_ex = '\n\n'.join(f"### {w['path']}\n{grep_excerpts(w['text'], terms) or w['text'][:1500]}"
                            for w in workfiles)
        prompt = fill(trace_tmpl, RUBRIC_ID=rubric['id'], RUBRIC_TITLE=rubric['title'],
                      MATCH_CRITERIA=rubric['match_criteria'],
                      JUDGE_REASONING=f"{page['score'].get('judge_model')}: {rubric['reasoning']}",
                      STATIONS=', '.join(stations), AGENT_SUMMARIES=summaries,
                      DOC_EXCERPTS=doc_ex or '(no source documents available)',
                      WORKFILE_EXCERPTS=wf_ex or '(no working files recovered)')
        return call_claude(client, args, prompt, TRACE_SCHEMA, f"trace:{rubric['id']}")

    traces = run_parallel(trace, failed, args.workers, 'traces')
    json.dump({'summaries': summaries, 'traces': traces}, open(out / 'analysis.json', 'w'), indent=1)
    print(f'traces: {len(traces)}/{len(failed)} -> analysis.json')

    # ---- phase C (optional): concept audit ----
    if args.concepts:
        pulls = json.loads((ex / 'concept-pulls.json').read_text())
        audit_tmpl, synth_tmpl = load_prompt('concept_audit'), load_prompt('concept_synthesis')

        def audit(item):
            name, tr = item
            prompt = fill(audit_tmpl, AGENT_NAME=name, RUN_CONTEXT=ctx,
                          CONCEPT_PULLS=pulls.get(name, {}),
                          TRANSCRIPT_JSON=json.dumps(tr['messages'])[:MAX_TRANSCRIPT_CHARS],
                          FINAL_ANSWER=tr.get('final_answer', ''))
            return call_claude(client, args, prompt, CONCEPT_AUDIT_SCHEMA, f'concepts:{name}')

        audits = run_parallel(audit, list(agents.items()), args.workers, 'concept-audits')
        json.dump({'per_agent': audits, 'synthesis': {}},
                  open(out / 'concepts-analysis.json', 'w'), indent=1)
        synth = call_claude(client, args,
                            fill(synth_tmpl, RUN_CONTEXT=ctx, PER_AGENT_AUDITS=audits),
                            CONCEPT_SYNTH_SCHEMA, 'concepts:synthesis')
        json.dump({'per_agent': audits, 'synthesis': synth},
                  open(out / 'concepts-analysis.json', 'w'), indent=1)
        print(f'concept audit: {len(audits)}/{len(agents)} -> concepts-analysis.json')


if __name__ == '__main__':
    main()
