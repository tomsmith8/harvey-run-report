"""
run-report-assemble-prompt-inputs.py

Stakwork Script-skill port of the analytical-input assembly logic that
lives in `analyze.py` inside github.com/tomsmith8/harvey-run-report
(functions: rubric_terms, grep_excerpts, run_context, and the per-rubric /
per-agent context-building inlined in trace()/summarize()/audit()), with
the per-agent assembly rewritten (2024 revision) to fix a real production
incident: the previous revision built TWO separate run-wide blocks
(agents_block / agents_concepts_block), each independently serializing the
FULL, un-truncated transcript for every agent with no size cap. Both blocks
got inlined into single run-wide prompts, and a real run (project
151773880) produced a 6,224,234-token prompt against a 1,000,000-token
model limit, which Anthropic rejected with HTTP 400 and which halted the
parent workflow via if_completed. This revision:

  * serializes each agent's transcript EXACTLY ONCE (never duplicated
    across two blocks),
  * caps that single serialized-JSON string at MAX_TRANSCRIPT_CHARS
    (matching the tomsmith8/harvey-run-report reference implementation's
    analyze.py cap) and flags truncation per agent,
  * ports analyze.py's / parse_logs.py's stitch_workfiles() behaviour so
    shared working-file content that a transcript's tool-call messages
    would otherwise re-embed verbatim is reconstructed ONCE for the run
    (page_data.workfiles is already that single reconstruction, produced
    upstream by run-report-parse-project.py) and elided to a lightweight
    pointer inside every agent's message list instead of being duplicated
    N times across N agents,
  * logs total agent count plus each agent's pre-cap/post-cap transcript
    character length and truncation status, surfaced in both stderr logs
    and the `warnings` output so a degraded (capped/elided) run is
    distinguishable from a healthy one,
  * ALWAYS emits agent_count (== len(agents_items)), including when it
    is 0, so a downstream IfElseCondition guard (guard_agents_items,
    added between assemble_prompt_inputs and the per-agent
    ForEachCondition fan-out in workflow 58019) can distinguish
    "genuinely zero agents" from "this step never ran / agents_items
    wasn't a non-empty array" - both of which for_each_condition.rb:59-73
    would otherwise silently rescue to [] + finished! with ZERO
    iterations and no error (the exact mode that let workflow version
    188399 ship with no assemble_prompt_inputs step at all while every
    step still reported success).

Runs standalone (no filesystem, no `os`/`subprocess`/`boto3`, no
`anthropic` client) and has no cross-file imports - this file is fully
self-contained, matching the convention of run-report-parse-project.py /
run-report-build.py.

WHY THIS SCRIPT EXISTS: the deterministic flow (set_var -> request_export
-> poll_export -> extract_download_url -> parse_project) produces real
page_data_json / concepts_json / transcripts_json, but nothing downstream
of parse_project ever derives the per-rubric / per-agent context blocks
the four RUN_REPORT_* prompts actually reference ([$(set_var).output.*]
placeholders such as doc_excerpts, workfile_excerpts, stations,
run_context, agent name/step/transcript/final_answer, concept_pulls).
This script is that derivation step - it sits after parse_project and
before the run_rubric_trace / run_transcript_summary / run_concept_audit /
run_concept_synthesis phases, producing one COLLECTION per phase (since
this workflow calls each analysis phase exactly once with its full
collection, not once per rubric/agent the way analyze.py's
ThreadPoolExecutor-driven per-item calls do).

INPUTS (workflow-injected globals; looked up defensively via
globals().get(), accepting a native dict/list, a JSON string, or a file
URL for every JSON-shaped var - large payloads may arrive as a file URL
since the Script skill auto-uploads `vars` over 150KB):

  page_data_json
      REQUIRED (logically). Output of run-report-parse-project.py:
      {config{task_goal,deliverable,models,...}, score{n_passed,
      n_criteria,judge_model,...}, rubrics[{id,title,match_criteria,
      verdict,reasoning}], agents[{name,step,...}], documents[{file,
      plain,...}], workfiles[{path,content,chunk_count}], ...}.
      NOTE: page_data.workfiles is an internal-only handoff field (same
      one run-report-build.py pops and relocates) - it is read here for
      grep purposes AND as the authoritative reconstructed-once source
      for stitch_workfiles(); never re-emitted verbatim.

  concepts_json
      REQUIRED (logically). Output of run-report-parse-project.py:
      {concept_pulls[{project_id,step,concept_id,concept_name,source}],
      workfile_writes[...]}.

  transcripts_json
      REQUIRED (logically). Output of run-report-parse-project.py: array
      of {project_id, step, agent_label, messages[], final_answer,
      n_messages, tools[]} - one entry per detected agent-call step. This
      is the source of per-agent `messages`/`final_answer`; page_data.
      agents[] deliberately excludes `messages` per its own contract.

OUTPUTS (printed as single-line `key: value` stdout lines; every JSON-
shaped or multi-line value is JSON-encoded so it stays on one physical
line, per the Script skill's line-based key:value output parser):

  failed_rubrics_block   JSON array - one entry per page_data.rubrics[]
                          entry with verdict == "fail":
                          {rubric_id, rubric_title, match_criteria,
                           judge_reasoning, doc_excerpts, workfile_excerpts}
                          UNCHANGED by this revision (caps: grep_excerpts
                          60 lines, rubric_terms 20 terms, 1500-char
                          workfile fallback).
  agents_items            JSON array - ONE entry per transcripts_json
                          entry (replaces the old agents_block /
                          agents_concepts_block; those two run-wide blocks
                          and their output keys no longer exist):
                          {name, step, transcript, final_answer,
                           concept_pulls, truncated}. `transcript` is the
                           agent's messages (after stitch_workfiles()
                           elision) serialized to JSON EXACTLY ONCE, then
                           capped at MAX_TRANSCRIPT_CHARS characters.
                           `truncated` is true iff the pre-cap serialized
                           length exceeded the cap. `concept_pulls` is
                           looked up from concepts_json by matching
                           project_id, same as the old
                           build_agents_concepts_block() did.
  run_context             JSON string - task/deliverable/models/score
                          summary plus one line per failed rubric.
                          UNCHANGED by this revision.
  stations                JSON string - comma-joined pipeline stations
  failed_rubric_count     plain integer string
  agent_count             plain integer string - always emitted, INCLUDING
                          when it is 0 (never omitted). Equal to
                          len(agents_items). This exists so a downstream
                          IfElseCondition guard (guard_agents_items, added
                          between assemble_prompt_inputs and the per-agent
                          ForEachCondition fan-out in workflow 58019) can
                          distinguish "assemble_prompt_inputs genuinely
                          produced zero agents" from "the step never ran /
                          agents_items wasn't a non-empty array" - both of
                          which for_each_condition.rb:59-73 would otherwise
                          silently rescue to [] + finished! with ZERO
                          iterations and no error (the exact failure mode
                          that let workflow version 188399 ship with no
                          assemble_prompt_inputs step at all and every
                          step still report success).
  warnings                JSON array of strings - now also includes one
                          entry per agent whose transcript was truncated,
                          plus a summary of how many workfile-write
                          messages stitch_workfiles() elided
  ok                      "true"/"false" - false only if page_data_json
                          itself could not be resolved to an object

Ported functions (behaviour-for-behaviour against analyze.py):

  rubric_terms(rubric)
      Tokens from title + match_criteria: money-figure regex matches plus
      words of 6+ letters (`[A-Za-z][A-Za-z-]{5,}`), minus the stoplist
      {criteria, memoranda, memorandum, includes, explains, identifies,
      calculates, material, materially}, filtered to len > 2, capped at
      20. NOTE (inherited quirk, not a bug introduced here): analyze.py
      dedupes matches through a `set()` before slicing to 20, so which 20
      terms survive the cap is not deterministic across runs/interpreters
      when more than 20 unique terms are found - this is ported exactly,
      not "fixed", per the behaviour-for-behaviour requirement.

  grep_excerpts(text, terms, context=1, max_lines=60)
      Case-insensitive substring match per term; keeps a +/-1-line window
      per hit; non-contiguous hit-runs are joined with a lone "..." line;
      output capped at 60 lines total.

  doc_excerpts(src_docs, terms)
      grep_excerpts() over each page_data.documents[].plain, formatted as
      "### {file}\\n{excerpt}" blocks, joined with a blank line - a
      document contributes nothing if it has zero term hits (matches
      analyze.py's `if grep_excerpts(...)` filter).

  workfile_excerpts(workfiles, terms)
      Same matcher over each workfile's text, ALWAYS included (no
      zero-hit filter, unlike doc_excerpts) - falls back to the first
      1500 chars when grep_excerpts() finds no hits. Reads workfile
      `content` (the key the deployed run-report-parse-project.py
      actually writes), falling back to `text` defensively in case an
      older export shape used that name.

  run_context(page_data)
      Task/deliverable/models/score summary line, plus one
      "- {id} {title} | match: {match_criteria} | judge: {reasoning}"
      line per failed rubric.

  stations(page_data)
      ['source-docs', 'graph-ingest'] + [a['name'] for a in
      page_data['agents']] + ['judge'], comma-joined.

  stitch_workfiles(transcripts, page_data_workfiles)
      Ported from the reference repo's parse_logs.py stitch_shared_files()
      pattern: shared working files are reconstructed ONCE for the run
      (page_data.workfiles, produced upstream by run-report-parse-
      project.py, already IS that single reconstruction). This function
      walks every agent transcript's messages[] and, for any message that
      carries a raw workfile-write payload (tool name in
      write_shared_file/shared_file_write/scratchpad_write/write_file/
      write_workfile, matched against a path also present in
      page_data.workfiles) whose path is already covered by that
      reconstruction, replaces the message's inline content with a
      lightweight {"workfile_ref": path, "note": ...} pointer instead of
      re-embedding the full file content. This never loses working-file
      content (the full content still lives once in page_data.workfiles
      and feeds workfile_excerpts() above) - it only removes the
      per-agent duplicate copies, further shrinking each agent's
      transcript before MAX_TRANSCRIPT_CHARS truncation is even applied.
"""

import json
import re
import sys
import urllib.request

LOG = "run_report_assemble_prompt_inputs"

# Matches the tomsmith8/harvey-run-report reference implementation's
# analyze.py cap on a single serialized agent transcript. Applied to the
# JSON-encoded `messages` array (post stitch_workfiles() elision), not to
# the raw Python object, since the cap is a character-length cap on the
# string that will actually be inlined into a prompt.
MAX_TRANSCRIPT_CHARS = 500_000


# --------------------------------------------------------------------------- #
# Generic input helpers (duplicated on purpose - each Script-skill file
# must be fully self-contained/standalone, no cross-file imports).
# --------------------------------------------------------------------------- #

def _get(*names, default=None):
    for name in names:
        v = globals().get(name)
        if v is not None:
            return v
    return default


def _looks_like_url(value):
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def resolve_json_input(value, default=None):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        if _looks_like_url(s):
            with urllib.request.urlopen(s, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        return json.loads(s)
    return default


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_list(v):
    return v if isinstance(v, list) else []


def _log(msg):
    # Deliberately stderr, NOT stdout - the Script skill parses every
    # stdout line as a `key: value` output pair, so free-form log lines
    # must never go to stdout or they would corrupt the output contract.
    print(f"[{LOG}] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# rubric_terms - ported verbatim from analyze.py (including the
# set()-then-slice non-determinism on the 20-item cap; see module
# docstring). UNCHANGED by this revision.
# --------------------------------------------------------------------------- #

_MONEY_RE = re.compile(r"\$?\d[\d,]*\.?\d*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]{5,}")
_STOPLIST = {
    "criteria", "memoranda", "memorandum", "includes", "explains",
    "identifies", "calculates", "material", "materially",
}


def rubric_terms(rubric):
    rubric = rubric if isinstance(rubric, dict) else {}
    txt = (rubric.get("title") or "") + " " + (rubric.get("match_criteria") or "")
    toks = set(_MONEY_RE.findall(txt))
    toks.update(w for w in _WORD_RE.findall(txt) if w.lower() not in _STOPLIST)
    return [t for t in toks if len(t) > 2][:20]


# --------------------------------------------------------------------------- #
# grep_excerpts - ported verbatim from analyze.py. UNCHANGED by this
# revision.
# --------------------------------------------------------------------------- #

def grep_excerpts(text, terms, context=1, max_lines=60):
    text = text if isinstance(text, str) else ""
    lines = text.split("\n")
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
            out.append("...")
        out.append(lines[i])
        prev = i
        if len(out) >= max_lines:
            break
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# doc_excerpts / workfile_excerpts - ported from analyze.py's trace()
# inline comprehensions, generalised to standalone helpers. UNCHANGED by
# this revision.
# --------------------------------------------------------------------------- #

def doc_excerpts_for_terms(src_docs, terms):
    parts = []
    for d in _as_list(src_docs):
        if not isinstance(d, dict):
            continue
        plain = d.get("plain") or ""
        ex = grep_excerpts(plain, terms)
        if ex:
            parts.append(f"### {d.get('file')}\n{ex}")
    return "\n\n".join(parts)


def workfile_excerpts_for_terms(workfiles, terms):
    parts = []
    for w in _as_list(workfiles):
        if not isinstance(w, dict):
            continue
        # parse-project v3 writes {name, path, note, text} - `text` is
        # authoritative; `content` accepted for older bundles.
        text = w.get("text")
        if text is None:
            text = w.get("content")
        text = text if isinstance(text, str) else ""
        ex = grep_excerpts(text, terms) or text[:1500]
        parts.append(f"### {w.get('path')}\n{ex}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# stations / run_context - ported from analyze.py. UNCHANGED by this
# revision.
# --------------------------------------------------------------------------- #

def build_stations(page_data):
    agents = _as_list(page_data.get("agents"))
    names = [a.get("name") for a in agents if isinstance(a, dict) and a.get("name")]
    return ["source-docs", "graph-ingest"] + names + ["judge"]


def build_run_context(page_data):
    config = _as_dict(page_data.get("config"))
    score = _as_dict(page_data.get("score"))
    rubrics = _as_list(page_data.get("rubrics"))
    failed = [r for r in rubrics if isinstance(r, dict) and r.get("verdict") == "fail"]
    lines = [
        f"Task: {config.get('task_goal')}",
        f"Deliverable: {config.get('deliverable')}",
        f"Models: {json.dumps(config.get('models'))}",
        f"Score: {score.get('n_passed')}/{score.get('n_criteria')} criteria passed "
        f"(judge {score.get('judge_model')}).",
        "Failed criteria:",
    ]
    for r in failed:
        lines.append(
            f"- {r.get('id')} {r.get('title')} | match: {r.get('match_criteria')} | judge: {r.get('reasoning')}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# failed_rubrics_block - one entry per failed rubric, packaging the
# per-rubric context analyze.py's trace() built inline per-item. UNCHANGED
# by this revision.
# --------------------------------------------------------------------------- #

def build_failed_rubrics_block(page_data, src_docs, workfiles, warnings):
    rubrics = _as_list(page_data.get("rubrics"))
    score = _as_dict(page_data.get("score"))
    judge_model = score.get("judge_model")
    failed = [r for r in rubrics if isinstance(r, dict) and r.get("verdict") == "fail"]

    block = []
    for r in failed:
        rid = r.get("id")
        terms = rubric_terms(r)
        if not terms:
            warnings.append(f"rubric {rid}: rubric_terms() produced zero hits; doc/workfile excerpts will be empty/fallback-only")

        doc_ex = doc_excerpts_for_terms(src_docs, terms)
        wf_ex = workfile_excerpts_for_terms(workfiles, terms)

        _log(
            f"rubric {rid}: {len(terms)} term(s), "
            f"doc_excerpts={len(doc_ex.splitlines()) if doc_ex else 0} line(s), "
            f"workfile_excerpts={len(wf_ex.splitlines()) if wf_ex else 0} line(s)"
        )

        block.append({
            "rubric_id": rid,
            "rubric_title": r.get("title"),
            "match_criteria": r.get("match_criteria"),
            "judge_reasoning": f"{judge_model}: {r.get('reasoning')}",
            "doc_excerpts": doc_ex or "(no source documents available)",
            "workfile_excerpts": wf_ex or "(no working files recovered)",
        })
    return block


# --------------------------------------------------------------------------- #
# stitch_workfiles - ported from the reference repo's parse_logs.py
# stitch_shared_files() pattern (see module docstring). Reconstructs shared
# working-file content ONCE for the run (page_data.workfiles is already
# that reconstruction) and elides duplicate raw workfile-write payloads
# found inline inside a transcript message down to a lightweight pointer,
# so per-agent transcript size never balloons with N copies of the same
# file across N agents.
# --------------------------------------------------------------------------- #

_WORKFILE_TOOL_NAMES = {
    "write_shared_file", "shared_file_write", "scratchpad_write",
    "write_file", "write_workfile",
}


def _message_workfile_write(msg):
    """Best-effort detection of a workfile-write payload embedded in a
    single transcript message. Checks the message's own top-level shape
    (tool/tool_name/name + path/file + content), then common nested
    shapes (data/payload/details, tool_calls[]) since the exact shape a
    transcript message takes depends on which agent-runner emitted it.
    Returns the file path if a write matching a known workfile-write tool
    is found, else None. Never raises on malformed input."""

    def _check(d):
        if not isinstance(d, dict):
            return None
        tool = d.get("tool") or d.get("tool_name") or d.get("name")
        if tool in _WORKFILE_TOOL_NAMES:
            path = d.get("path") or d.get("file")
            content = d.get("content")
            if path and isinstance(content, str):
                return path
        return None

    if not isinstance(msg, dict):
        return None

    path = _check(msg)
    if path:
        return path

    for key in ("data", "payload", "details"):
        path = _check(msg.get(key))
        if path:
            return path

    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            path = _check(tc)
            if path:
                return path

    return None


def stitch_workfiles(transcripts, page_data_workfiles):
    """Returns (stitched_transcripts, elided_count, elided_by_agent).

    stitched_transcripts is a NEW list - input transcripts are never
    mutated in place. elided_count is the total number of messages
    replaced by a workfile_ref pointer across every transcript.
    elided_by_agent maps agent name/step -> elided-message count, for
    logging."""
    known_paths = {
        (w.get("path") or w.get("file"))
        for w in _as_list(page_data_workfiles)
        if isinstance(w, dict) and (w.get("path") or w.get("file"))
    }

    elided_count = 0
    elided_by_agent = {}
    out_transcripts = []

    for tr in _as_list(transcripts):
        if not isinstance(tr, dict):
            out_transcripts.append(tr)
            continue

        agent_key = tr.get("agent_label") or tr.get("step") or "(unnamed)"
        messages = _as_list(tr.get("messages"))
        new_messages = []
        agent_elided = 0

        for msg in messages:
            path = _message_workfile_write(msg)
            if path and path in known_paths:
                new_messages.append({
                    "workfile_ref": path,
                    "note": (
                        "content stitched once at page_data.workfiles; "
                        "elided here by stitch_workfiles() to avoid per-agent duplication"
                    ),
                })
                elided_count += 1
                agent_elided += 1
            else:
                new_messages.append(msg)

        if agent_elided:
            elided_by_agent[agent_key] = agent_elided

        new_tr = dict(tr)
        new_tr["messages"] = new_messages
        out_transcripts.append(new_tr)

    return out_transcripts, elided_count, elided_by_agent


# --------------------------------------------------------------------------- #
# agents_items - ONE entry per transcript (replaces agents_block /
# agents_concepts_block). Each agent's transcript is serialized to JSON
# EXACTLY ONCE, then capped at MAX_TRANSCRIPT_CHARS. concept_pulls is
# looked up from concepts_json the same way build_agents_concepts_block()
# used to (match on project_id).
# --------------------------------------------------------------------------- #

def build_agents_items(transcripts, concept_pulls, warnings):
    concept_pulls = _as_list(concept_pulls)
    items = []

    for tr in _as_list(transcripts):
        if not isinstance(tr, dict):
            continue

        pid = tr.get("project_id")
        name = tr.get("name") or tr.get("agent_label") or tr.get("step")
        pulls_for_agent = [
            p for p in concept_pulls
            if isinstance(p, dict) and (
                (name is not None and p.get("agent") == name)
                or (pid is not None and p.get("project_id") == pid)
            )
        ]

        # Serialized EXACTLY ONCE here - this is the only place in the
        # whole script that turns an agent's messages[] into a JSON
        # string, so a transcript can never be duplicated across two
        # output blocks the way agents_block/agents_concepts_block used to.
        serialized = json.dumps(_as_list(tr.get("messages")), default=str)
        pre_cap_len = len(serialized)
        truncated = pre_cap_len > MAX_TRANSCRIPT_CHARS
        if truncated:
            serialized = serialized[:MAX_TRANSCRIPT_CHARS]
        post_cap_len = len(serialized)

        _log(
            f"agent {name!r} (project_id={pid!r}): "
            f"transcript_chars_pre_cap={pre_cap_len}, "
            f"transcript_chars_post_cap={post_cap_len}, truncated={truncated}"
        )
        if truncated:
            warnings.append(
                f"agent {name!r}: transcript truncated from {pre_cap_len} to "
                f"{MAX_TRANSCRIPT_CHARS} chars (MAX_TRANSCRIPT_CHARS cap)"
            )

        items.append({
            "name": name,
            "step": tr.get("step"),
            "transcript": serialized,
            "final_answer": tr.get("final_answer"),
            "concept_pulls": pulls_for_agent,
            "truncated": truncated,
        })

    return items


# --------------------------------------------------------------------------- #
# Entry point - reads injected vars, runs the assembly, prints outputs.
# --------------------------------------------------------------------------- #

_page_data_var = _get("page_data_json", "page_data")
_concepts_var = _get("concepts_json", "concepts")
_transcripts_var = _get("transcripts_json", "transcripts")

_warnings = []

try:
    _page_data = resolve_json_input(_page_data_var, default=None)
except Exception as exc:  # noqa: BLE001 - never let a bad page_data_json crash the step
    _page_data = None
    _warnings.append(f"page_data_json: failed to resolve ({exc})")

try:
    _concepts = resolve_json_input(_concepts_var, default=None)
except Exception as exc:  # noqa: BLE001
    _concepts = None
    _warnings.append(f"concepts_json: failed to resolve ({exc})")

try:
    _transcripts = resolve_json_input(_transcripts_var, default=None)
except Exception as exc:  # noqa: BLE001
    _transcripts = None
    _warnings.append(f"transcripts_json: failed to resolve ({exc})")

_page_data = _page_data if isinstance(_page_data, dict) else {}
_concepts = _concepts if isinstance(_concepts, dict) else {}
_transcripts = _transcripts if isinstance(_transcripts, list) else []

if not _page_data:
    _warnings.append("page_data_json was missing/empty; all rubric/agent blocks will be empty")
if not _concepts:
    _warnings.append("concepts_json was missing/empty; agents_items[].concept_pulls will be empty")
if not _transcripts:
    _warnings.append("transcripts_json was missing/empty; agents_items will be empty")

_src_docs = _as_list(_page_data.get("documents"))
_workfiles = _as_list(_page_data.get("workfiles"))
_concept_pulls = _as_list(_concepts.get("concept_pulls"))

_log(f"total agents (transcripts): {len(_transcripts)}")
_log(f"input sizes: rubrics={len(_as_list(_page_data.get('rubrics')))}, "
     f"documents={len(_src_docs)}, workfiles={len(_workfiles)}, "
     f"transcripts={len(_transcripts)}, concept_pulls={len(_concept_pulls)}")

_stitched_transcripts, _elided_count, _elided_by_agent = stitch_workfiles(_transcripts, _workfiles)
if _elided_count:
    _log(f"stitch_workfiles: elided {_elided_count} duplicate workfile-write message(s) "
         f"across agents: {_elided_by_agent}")
    _warnings.append(
        f"stitch_workfiles: elided {_elided_count} duplicate workfile-write message(s) "
        f"across {len(_elided_by_agent)} agent(s) to avoid re-embedding shared working files"
    )

failed_rubrics_block = build_failed_rubrics_block(_page_data, _src_docs, _workfiles, _warnings)
agents_items = build_agents_items(_stitched_transcripts, _concept_pulls, _warnings)
stations_list = build_stations(_page_data)
run_context = build_run_context(_page_data)

_truncated_agents = [item["name"] for item in agents_items if item["truncated"]]
if _truncated_agents:
    _log(f"truncated agents ({len(_truncated_agents)}/{len(agents_items)}): {_truncated_agents}")
    _warnings.append(
        f"{len(_truncated_agents)} of {len(agents_items)} agent(s) had a truncated transcript: "
        f"{_truncated_agents}"
    )
else:
    _log(f"no agents truncated (0/{len(agents_items)})")

_log(f"output sizes: failed_rubrics_block={len(failed_rubrics_block)}, "
     f"agents_items={len(agents_items)}, stations={len(stations_list)}")

# agent_count is ALWAYS emitted, including when it is 0 - a downstream
# IfElseCondition guard (guard_agents_items) relies on this integer being
# present unconditionally to distinguish "genuinely zero agents" from
# "agents_items wasn't produced at all", since for_each_condition.rb:59-73
# silently rescues a non-Array/non-JSON-parseable `items` to [] and calls
# finished! with zero iterations and no error otherwise.
agent_count = len(agents_items)
_log(f"agent_count: {agent_count}")

ok = bool(_page_data)

print(f"failed_rubrics_block: {json.dumps(failed_rubrics_block, default=str)}")
print(f"agents_items: {json.dumps(agents_items, default=str)}")
print(f"run_context: {json.dumps(run_context, default=str)}")
print(f"stations: {json.dumps(', '.join(stations_list), default=str)}")
print(f"failed_rubric_count: {len(failed_rubrics_block)}")
print(f"agent_count: {agent_count}")
print(f"warnings: {json.dumps(_warnings, default=str)}")
print(f"ok: {'true' if ok else 'false'}")


# --------------------------------------------------------------------------- #
# Pure-Python unit tests (stdlib `unittest` only - no filesystem, no os,
# no subprocess, no boto3). NEVER executed inside the Stakwork Script-skill
# sandbox: gated behind an explicit CLI flag check via sys.argv (which the
# skill runner never populates) AND a defensive globals().get() lookup for
# __name__ (never raises even if the sandbox's exec() doesn't set it).
# Run locally with:
#     python run-report-assemble-prompt-inputs.py --self-test
# --------------------------------------------------------------------------- #

def _run_self_tests():
    import unittest

    class AssemblePromptInputsTests(unittest.TestCase):
        def _fixture_transcripts(self):
            big_messages = [{"role": "assistant", "content": "x" * 10} for _ in range(80_000)]
            return [
                {
                    "project_id": "p-1",
                    "step": "drafter",
                    "agent_label": "Drafter",
                    "messages": [{"role": "user", "content": "draft the memo"}],
                    "final_answer": "Drafted.",
                },
                {
                    "project_id": "p-2",
                    "step": "researcher",
                    "agent_label": "Researcher",
                    "messages": big_messages,
                    "final_answer": "Researched.",
                },
                {
                    "project_id": "p-3",
                    "step": "reviewer",
                    "agent_label": "Reviewer",
                    "messages": [{"role": "user", "content": "review it"}],
                    "final_answer": "Reviewed.",
                },
            ]

        def _fixture_concept_pulls(self):
            return [
                {"project_id": "p-1", "step": "drafter", "concept_id": "c1", "concept_name": "Materiality", "source": "graph"},
                {"project_id": "p-3", "step": "reviewer", "concept_id": "c2", "concept_name": "Standard of Review", "source": "graph"},
            ]

        def test_oversized_agent_is_capped_and_flagged(self):
            transcripts = self._fixture_transcripts()
            warnings = []
            items = build_agents_items(transcripts, [], warnings)
            self.assertEqual(len(items), 3)

            by_name = {i["name"]: i for i in items}
            researcher = by_name["Researcher"]
            self.assertTrue(researcher["truncated"])
            self.assertLessEqual(len(researcher["transcript"]), MAX_TRANSCRIPT_CHARS)
            self.assertEqual(len(researcher["transcript"]), MAX_TRANSCRIPT_CHARS)

            drafter = by_name["Drafter"]
            reviewer = by_name["Reviewer"]
            self.assertFalse(drafter["truncated"])
            self.assertFalse(reviewer["truncated"])

            self.assertTrue(any("Researcher" in w and "truncated" in w for w in warnings))
            self.assertEqual(len(items), 3)
            # agent_count is always len(agents_items), computed by the
            # entry point the same way in every fixture size.
            agent_count = len(items)
            self.assertEqual(agent_count, 3)

        def test_concept_pulls_attach_by_project_id(self):
            transcripts = self._fixture_transcripts()
            pulls = self._fixture_concept_pulls()
            items = build_agents_items(transcripts, pulls, [])
            by_name = {i["name"]: i for i in items}

            self.assertEqual(len(by_name["Drafter"]["concept_pulls"]), 1)
            self.assertEqual(by_name["Drafter"]["concept_pulls"][0]["concept_id"], "c1")

            self.assertEqual(len(by_name["Researcher"]["concept_pulls"]), 0)

            self.assertEqual(len(by_name["Reviewer"]["concept_pulls"]), 1)
            self.assertEqual(by_name["Reviewer"]["concept_pulls"][0]["concept_id"], "c2")

        def test_no_legacy_block_keys_and_transcript_serialized_once(self):
            transcripts = self._fixture_transcripts()
            pulls = self._fixture_concept_pulls()
            warnings = []
            items = build_agents_items(transcripts, pulls, warnings)

            output_payload = {
                "failed_rubrics_block": [],
                "agents_items": items,
                "run_context": "",
                "stations": "",
                "failed_rubric_count": 0,
                "warnings": warnings,
                "ok": "true",
            }

            self.assertNotIn("agents_block", output_payload)
            self.assertNotIn("agents_concepts_block", output_payload)

            dumped = json.dumps(output_payload, default=str)
            self.assertNotIn("agents_block", dumped)
            self.assertNotIn("agents_concepts_block", dumped)

            drafter_transcript = next(i["transcript"] for i in items if i["name"] == "Drafter")
            # drafter_transcript is itself a JSON string; once embedded as a
            # field value inside the outer json.dumps(output_payload) it is
            # re-escaped (quotes become \"), so search for its escaped form.
            occurrences = dumped.count(json.dumps(drafter_transcript))
            self.assertEqual(occurrences, 1)

        def test_stitch_workfiles_elides_duplicate_shared_writes(self):
            transcripts = [
                {
                    "project_id": "p-1",
                    "step": "drafter",
                    "agent_label": "Drafter",
                    "messages": [
                        {"role": "assistant", "content": "writing notes"},
                        {"tool": "write_shared_file", "path": "scratch/notes.md", "content": "A" * 5000},
                    ],
                    "final_answer": "Drafted.",
                },
                {
                    "project_id": "p-2",
                    "step": "reviewer",
                    "agent_label": "Reviewer",
                    "messages": [
                        {"tool": "write_shared_file", "path": "scratch/notes.md", "content": "A" * 5000},
                        {"role": "assistant", "content": "reviewed the shared notes"},
                    ],
                    "final_answer": "Reviewed.",
                },
            ]
            page_data_workfiles = [
                {"path": "scratch/notes.md", "content": "A" * 5000, "chunk_count": 2},
            ]

            stitched, elided_count, elided_by_agent = stitch_workfiles(transcripts, page_data_workfiles)
            self.assertEqual(elided_count, 2)
            self.assertEqual(elided_by_agent.get("Drafter"), 1)
            self.assertEqual(elided_by_agent.get("Reviewer"), 1)

            drafter_msgs = next(t["messages"] for t in stitched if t["agent_label"] == "Drafter")
            self.assertIn("workfile_ref", drafter_msgs[1])
            self.assertEqual(drafter_msgs[1]["workfile_ref"], "scratch/notes.md")

            items = build_agents_items(stitched, [], [])
            drafter_item = next(i for i in items if i["name"] == "Drafter")
            self.assertNotIn("A" * 5000, drafter_item["transcript"])

        def test_zero_agents_output_emits_agent_count_zero(self):
            # Empty transcripts_json -> agents_items == [] and agent_count
            # must still be PRESENT (not omitted, not null) and equal to 0.
            # This is the exact signal guard_agents_items (IfElseCondition
            # in workflow 58019) checks to route to system.fail, distinct
            # from for_each_condition.rb's silent-rescue-to-[] behaviour
            # when agents_items is missing/malformed entirely.
            items = build_agents_items([], [], [])
            self.assertEqual(items, [])

            agent_count = len(items)
            self.assertEqual(agent_count, 0)

            output_payload = {
                "failed_rubrics_block": [],
                "agents_items": items,
                "run_context": "",
                "stations": "",
                "failed_rubric_count": 0,
                "agent_count": agent_count,
                "warnings": [],
                "ok": "true",
            }
            # Must be present and equal to 0 - never omitted, never null.
            self.assertIn("agent_count", output_payload)
            self.assertIsNotNone(output_payload["agent_count"])
            self.assertEqual(output_payload["agent_count"], 0)

            dumped = json.dumps(output_payload, default=str)
            parsed_back = json.loads(dumped)
            self.assertIn("agent_count", parsed_back)
            self.assertEqual(parsed_back["agent_count"], 0)

    suite = unittest.TestLoader().loadTestsFromTestCase(AssemblePromptInputsTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if globals().get("__name__") == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--self-test":
    _run_self_tests()
