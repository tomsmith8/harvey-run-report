"""
run-report-build.py (v2)

Stakwork Script-skill JOIN step: merges the deterministic structure from
run-report-parse-project.py with the four LLM phase outputs into the published
bundle Hive's run-report page consumes:

  { "schema_version": 1, "page_data": {...},
    "analysis": {"summaries": [], "traces": []},
    "concepts": {"per_agent": [], "synthesis": {...}},
    "source_docs": [{id, title, html}], "workfiles": [{name, text, ...}],
    "rubric_links": {rubric_id: [{doc, tokens}]} }

v2 contract fixes (verified against hive src/lib/run-report/types.ts +
project.ts, which this bundle feeds):

  rubric_links     was {rid: {title, verdict, trace}} - Hive's projection does
                   `if (!Array.isArray(value)) continue`, so every link was
                   dropped. Now the toolkit shape: {rid: [{doc, tokens, n}]},
                   computed here from each failed rubric's terms against the
                   documents' plain text (fed through page_data.documents[]
                   internal `plain`).
  source_docs      was page_data.documents metadata with `plain` stripped -
                   never had `html`, so the document viewer was empty. Now
                   [{id, title, html}] lifted from the documents' internal
                   plain/html handoff fields; both internal fields are stripped
                   from the published page_data.documents[].
  workfiles        parse now emits `text` (the key Hive's projection reads);
                   this script normalizes any legacy `content` key to `text`
                   and guarantees `name`.
  branches         coerced to plain strings (Hive narrows branches to string[];
                   objects were silently dropped).
  concepts         per_agent + synthesis as before; additionally passes through
                   the deterministic `by_agent` pulls under
                   concepts.deterministic_pulls so the page can show concept
                   activity even when the LLM audit did not run.

Inputs/outputs and the run_llm / analysis_mode / degraded semantics are
unchanged from v1 (page_data_json, run_llm, rubric_trace_json,
transcript_summary_json, concept_audit_json, concept_synthesis_json in;
report_data, warnings, llm, ok, analysis_mode, degraded out).
"""
import json
import re
import sys
import urllib.request

LOG = "run_report_build"


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


def truthy(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_list(v):
    return v if isinstance(v, list) else []


def _log(msg):
    print(f"[{LOG}] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Phase loading + content-quality checks (unchanged from v1)
# --------------------------------------------------------------------------- #

def empty_synthesis():
    return {"overall_narrative": None, "concept_matrix": [],
            "relation_to_failures": [], "recommendations": []}


def normalize_phase_items(items, label, warnings):
    """Keep only bare phase objects; wrapper records are rejected, not adapted.

    A mis-wired fan-out collection contains runner envelopes
    ({agent_name, status, result, error}) or fetch-step file pointers
    ({filename, text: <url>}) instead of the model replies. Those mean the
    WORKFLOW collected the wrong variable - this script reports the exact
    violation and refuses the records, so the fix lands in the workflow,
    never here.
    """
    bare, envelopes, pointers = [], 0, 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if set(it.keys()) <= {"filename", "text"} and str(it.get("text") or "").startswith("http"):
            pointers += 1
            continue
        if "result" in it and ("status" in it or "error" in it):
            envelopes += 1
            continue
        bare.append(it)
    if envelopes:
        warnings.append(f"{label}: {envelopes} runner-envelope record(s) "
                        "({agent_name,status,result,error}) rejected - the workflow "
                        "collected the runner reply wrapper; collect each iteration's "
                        "parsed `result` payload instead")
    if pointers:
        warnings.append(f"{label}: {pointers} file-pointer record(s) ({{filename,text}}) "
                        "rejected - a fetch/load step's own output leaked into the "
                        "phase collection")
    return bare


def drop_blank_items(items, fields, label, warnings):
    """Drop items whose content fields are all blank (e.g. empty schema echoes
    from a prompt that received no input), then dedupe by agent_name."""
    kept, seen = [], set()
    for it in items:
        if not any(not _is_blank(it.get(f)) for f in fields):
            continue
        name = it.get("agent_name")
        if name and name in seen:
            continue
        if name:
            seen.add(name)
        kept.append(it)
    n = len(items) - len(kept)
    if n:
        warnings.append(f"{label}: {n} blank/duplicate item(s) dropped")
    return kept


def load_phase_array(raw_value, label, warnings, content_fields=None):
    if raw_value is None:
        warnings.append(f"{label}: no content supplied; using empty shape")
        return [], False
    try:
        parsed = resolve_json_input(raw_value, default=None)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{label}: failed to resolve content ({exc}); using empty shape")
        return [], False
    if parsed is None:
        warnings.append(f"{label}: content resolved to nothing; using empty shape")
        return [], False
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        warnings.append(f"{label}: content was not an array/object; using empty shape")
        return [], False
    parsed = normalize_phase_items(parsed, label, warnings)
    if content_fields:
        parsed = drop_blank_items(parsed, content_fields, label, warnings)
    return parsed, True


def load_phase_object(raw_value, label, warnings):
    if raw_value is None:
        warnings.append(f"{label}: no content supplied; using empty shape")
        return empty_synthesis(), False
    try:
        parsed = resolve_json_input(raw_value, default=None)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{label}: failed to resolve content ({exc}); using empty shape")
        return empty_synthesis(), False
    if isinstance(parsed, list):
        parsed = normalize_phase_items(parsed, label, warnings)
        parsed = parsed[0] if len(parsed) >= 1 and isinstance(parsed[0], dict) else None
    if isinstance(parsed, dict) and "result" in parsed and ("status" in parsed or "error" in parsed):
        warnings.append(f"{label}: got a runner-envelope record, not the reply object - "
                        "the workflow must pass the parsed `result` payload")
        parsed = None
    if not isinstance(parsed, dict):
        warnings.append(f"{label}: content was not an object; using empty shape")
        return empty_synthesis(), False
    return parsed, True


def _is_blank(value):
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def all_blank(items, fields):
    items = _as_list(items)
    if not items:
        return True
    for item in items:
        if isinstance(item, dict):
            for f in fields:
                if not _is_blank(item.get(f)):
                    return False
    return True


# --------------------------------------------------------------------------- #
# rubric_links - the toolkit shape: {rubric_id: [{doc, tokens, n}]}, from
# rubric terms matched against document plain text.
# --------------------------------------------------------------------------- #

_STOP_NUMS = {"2010", "2020", "2021", "2022", "2023", "2024", "2025", "2026", "100", "1000"}


def rubric_tokens(rubric):
    txt = (rubric.get("title") or "") + " " + (rubric.get("match_criteria") or "")
    toks = set()
    for m in re.finditer(r"\$?(\d{1,2}[,.]\d{2,3}|\d{3,4}(?:,\d{3})?|\d+\.\d+)", txt):
        num = m.group(1)
        if num.replace(",", "") in _STOP_NUMS or num.startswith("0"):
            continue
        if "." in num or "," in num or len(num.replace(",", "")) >= 3:
            toks.add(num)
    for m in re.finditer(r"['\"]([A-Za-z][A-Za-z0-9 \-]{5,60})['\"]", txt):
        toks.add(m.group(1).lower())
    return toks


def build_rubric_links(rubrics, docs):
    plain = {d["id"]: (d.get("plain") or "").lower() for d in docs if isinstance(d, dict) and d.get("id")}
    links = {}
    for r in _as_list(rubrics):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        hits = []
        for doc_id, text in plain.items():
            if not text:
                continue
            matched = set()
            for t in rubric_tokens(r):
                variants = {t, t.replace(",", "")}
                if re.fullmatch(r"\d{4}", t):
                    variants.add(f"{t[0]},{t[1:]}")
                if any(v.lower() in text for v in variants):
                    matched.add(t)
            if matched:
                hits.append({"doc": doc_id, "tokens": sorted(matched)[:4], "n": len(matched)})
        hits.sort(key=lambda h: -h["n"])
        if hits:
            links[r["id"]] = hits[:5]
    return links


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def build_report_data(page_data_raw, run_llm, traces_raw, summaries_raw,
                      audit_raw, synthesis_raw):
    warnings = []
    page_data = _as_dict(page_data_raw)
    if not page_data:
        warnings.append("page_data was missing/empty; report_data.page_data will be an empty object")

    phases_run, phases_skipped, phases_failed = [], [], []
    if not run_llm:
        traces, summaries, per_agent, synthesis = [], [], [], empty_synthesis()
        phases_skipped = ["rubric-trace", "transcript-summary", "concept-audit", "concept-synthesis"]
        warnings.append("run_llm=false; all four analysis phases deliberately skipped")
    else:
        traces, ok_t = load_phase_array(traces_raw, "rubric-trace", warnings,
                                        ["root_cause", "classification"])
        if ok_t and all_blank(traces, ["root_cause"]):
            ok_t = False
            warnings.append("rubric-trace: parsed but every element has a blank root_cause")
        (phases_run if ok_t else phases_failed).append("rubric-trace")

        summaries, ok_s = load_phase_array(summaries_raw, "transcript-summary", warnings,
                                           ["mission", "context_gathered"])
        if ok_s and all_blank(summaries, ["mission", "context_gathered"]):
            ok_s = False
            warnings.append("transcript-summary: parsed but every element is blank")
        (phases_run if ok_s else phases_failed).append("transcript-summary")

        per_agent, ok_a = load_phase_array(audit_raw, "concept-audit", warnings,
                                           ["overall_assessment"])
        if ok_a and all_blank(per_agent, ["overall_assessment"]):
            ok_a = False
            warnings.append("concept-audit: parsed but every element is blank")
        (phases_run if ok_a else phases_failed).append("concept-audit")

        synthesis, ok_y = load_phase_object(synthesis_raw, "concept-synthesis", warnings)
        if ok_y and all_blank([synthesis], ["overall_narrative"]):
            ok_y = False
            warnings.append("concept-synthesis: parsed but overall_narrative is blank")
        (phases_run if ok_y else phases_failed).append("concept-synthesis")

        # a failed phase publishes EMPTY, never junk - Hive falls back to its
        # deterministic rendering instead of drawing wrapper records
        if not ok_t:
            traces = []
        if not ok_s:
            summaries = []
        if not ok_a:
            per_agent = []
        if not ok_y:
            synthesis = empty_synthesis()

    published = dict(page_data)

    # ---- source_docs lifted from documents' internal plain/html handoff ----
    raw_docs = _as_list(page_data.get("documents"))
    source_docs = [{"id": d.get("id"), "title": d.get("title") or d.get("file"),
                    "kind": d.get("kind") or "document", "file": d.get("file"),
                    "html": d.get("html") or ""}
                   for d in raw_docs if isinstance(d, dict) and (d.get("html") or "").strip()]
    rubric_links = build_rubric_links(page_data.get("rubrics"), raw_docs)
    published["documents"] = [{k: v for k, v in d.items() if k not in ("plain", "html")}
                              for d in raw_docs if isinstance(d, dict)]

    # ---- workfiles relocated + normalized to the `text` key ----------------
    workfiles = []
    for w in _as_list(published.pop("workfiles", [])):
        if not isinstance(w, dict):
            continue
        text = w.get("text")
        if not isinstance(text, str):
            text = w.get("content") if isinstance(w.get("content"), str) else ""
        entry = {k: v for k, v in w.items() if k != "content"}
        entry["text"] = text
        entry.setdefault("name", w.get("path") or "workfile")
        workfiles.append(entry)

    # ---- branches / health_notes must be plain strings ---------------------
    published["branches"] = [b if isinstance(b, str) else json.dumps(b)
                             for b in _as_list(published.get("branches"))]
    published["health_notes"] = [h if isinstance(h, str) else json.dumps(h)
                                 for h in _as_list(published.get("health_notes"))]

    concepts = {
        "per_agent": per_agent,
        "synthesis": {
            "overall_narrative": _as_dict(synthesis).get("overall_narrative"),
            "concept_matrix": _as_list(_as_dict(synthesis).get("concept_matrix")),
            "relation_to_failures": _as_list(_as_dict(synthesis).get("relation_to_failures")),
            "recommendations": _as_list(_as_dict(synthesis).get("recommendations")),
        },
    }

    report_data = {
        "schema_version": 1,
        "page_data": published,
        "analysis": {"summaries": summaries, "traces": traces},
        "concepts": concepts,
        "source_docs": source_docs,
        "workfiles": workfiles,
        "rubric_links": rubric_links,
    }

    llm = {"phases_run": phases_run, "phases_skipped": phases_skipped, "phases_failed": phases_failed}
    if not run_llm:
        analysis_mode = "deterministic"
    elif phases_failed:
        analysis_mode = "degraded"
    else:
        analysis_mode = "full"
    return report_data, warnings, llm, bool(page_data), analysis_mode


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

_load_warnings = []
try:
    _page_data = resolve_json_input(_get("page_data_json", "page_data"), default=None)
except Exception as exc:  # noqa: BLE001
    _page_data = None
    _load_warnings.append(f"page_data_json: failed to resolve ({exc})")

report_data, _merge_warnings, llm, ok, analysis_mode = build_report_data(
    _page_data,
    truthy(_get("run_llm"), default=True),
    _get("rubric_trace_json", "failure_trace_json", "rubric_traces_json"),
    _get("transcript_summary_json", "transcript_summaries_json"),
    _get("concept_audit_json", "concept_audits_json"),
    _get("concept_synthesis_json", "concept_synthesis"),
)

_warnings = _load_warnings + _merge_warnings
_log(f"source_docs={len(report_data['source_docs'])} workfiles={len(report_data['workfiles'])} "
     f"rubric_links={len(report_data['rubric_links'])} mode={analysis_mode}")

print(f"report_data: {json.dumps(report_data, default=str)}")
print(f"warnings: {json.dumps(_warnings, default=str)}")
print(f"llm: {json.dumps(llm, default=str)}")
print(f"ok: {'true' if ok else 'false'}")
print(f"analysis_mode: {analysis_mode}")
print(f"degraded: {'true' if analysis_mode == 'degraded' else 'false'}")
