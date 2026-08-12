"""
run-report-parse-project.py (v3)

Stakwork Script-skill port of harvey-run-report's parse_project.py, standalone
(stdlib only - no filesystem, os, subprocess, boto3; single file). Produces the
deterministic half of the report bundle that Hive's run-report page consumes
(src/lib/run-report/types.ts + project.ts).

v3 is a ground-up realignment with the toolkit + the Hive contract. What v2 got
wrong, per symptom:

  timeline noise / wrong names   v2 emitted one row per PROJECT NODE (root +
                                 every descendant, including 230+ per-agent
                                 child_poll_agent_request_id polling children).
                                 v3 emits one row per ROOT WORKFLOW STEP in
                                 export order (set_var, build_checklist_messages,
                                 ...), with launch-step times backfilled from
                                 child-project windows. Polling grandchildren
                                 are counted, never emitted.
  agent roster / gantt linking   v2 named agents after the step that carried
                                 messages ('send_agent_logs' x N) and emitted
                                 tools: ['unknown'] (it looked for OpenAI-style
                                 msg.tool_calls; these transcripts use
                                 content[].type == 'tool-call' with toolName).
                                 v3 names agents from the child_name suffix
                                 (run_draft -> draft), sets step = the launching
                                 root step (so the timeline row and the agent
                                 card link up), and counts real tools.
  workfiles empty                v2 looked for write_shared_file/scratchpad_write
                                 steps that do not exist in this pipeline. v3
                                 ports the toolkit's stitcher: shared files are
                                 reconstructed from the agents' own cat/sed/grep
                                 reads inside transcripts. Emitted with `text`
                                 (the key Hive's projection reads), not `content`.
  source documents empty         v2 never produced document text. v3 builds
                                 plain + html per document from the doc-ingest
                                 children's parse_document elements_json - no
                                 downloads, sandbox-safe. run-report-build.py
                                 lifts these into source_docs [{id,title,html}].
  security spam                  v2 blanked every credential-NAMED field
                                 (including unresolved {{TEMPLATE}} refs and
                                 events_token JWTs) and emitted one generic row
                                 per (project, step, kind). v3 ports the
                                 toolkit's value-based redaction: collect
                                 secret-shaped VALUES (with a {{...}} template
                                 guard and a letters+digits requirement), erase
                                 every occurrence at any escape depth, and
                                 report aggregated, readable findings.
  outputs strange                v2 set outputs = the root's ENTIRE per-step
                                 output dict. v3 sets it to the deliverable map
                                 (assemble_output_map outputs_json), {} if absent.
  branches unrenderable          v2 emitted objects; Hive narrows branches to
                                 string[]. v3 emits strings.
  concepts empty                 v2 looked for steps NAMED *concept*. v3 ports
                                 the toolkit's extractor: Concept/Doctrine/
                                 LegalArgument node pulls found in transcript
                                 tool calls/results.

INPUT (workflow-injected global):
  download_url   presigned GET URL for the (gzip or plain) ProjectTreeReport
                 JSON export.

OUTPUT (single-line `key: value` stdout pairs):
  page_data_json    config, score, rubrics[], timeline[], agents[], documents[]
                    (with internal-only plain/html per doc), branches[] (strings),
                    health_notes[] (strings), wall_clock_min, log_stats{},
                    security[], outputs{}, workfiles (internal handoff), degraded
  transcripts_json  [{name, project_id, step, agent_label, messages, final_answer,
                    n_messages, tools{name:count}}] - full messages, redacted
  concepts_json     {by_agent: {name: {queries[], nodes[]}}, concept_pulls: [...]}
  warnings          JSON array of strings
"""
import gzip
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LOG = "run_report_parse_project"


def _get(name, default=None):
    v = globals().get(name)
    return v if v is not None else default


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_list(v):
    return v if isinstance(v, list) else []


def _log(msg):
    print(f"[{LOG}] {msg}", file=sys.stderr)


def parse_maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return None
    return v


# --------------------------------------------------------------------------- #
# Timestamps: ISO8601 in -> "YYYY-MM-DD HH:MM:SS.mmm" UTC out (the format
# Hive's toEpochMs and the toolkit viewer both parse).
# --------------------------------------------------------------------------- #

def iso_to_dt(s):
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def fmt_ts(dt):
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def dur_s(a, b):
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 3)


# --------------------------------------------------------------------------- #
# Secret redaction - ported from the toolkit (value-based, template-guarded).
# Collect secret-shaped VALUES first, then erase every occurrence of each
# value at any JSON-escape depth. Unresolved {{TEMPLATE}} placeholders are
# never flagged. Runs over the raw export text before parsing.
# --------------------------------------------------------------------------- #

_AMP = r'(?:[?&]|\\u0026|&amp;)'
SECRET_PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "critical"),
    ("OpenAI-style API key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{30,}"), "critical"),
    ("LlamaParse API key", re.compile(r"llx-[A-Za-z0-9]{16,}"), "critical"),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}"), "critical"),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "high"),
    ("Query token", re.compile(_AMP + r"(?:run_)?(?:token|api_?key)=[A-Fa-f0-9]{32,}"), "high"),
    ("Service token in env dump", re.compile(r"\b[A-Z][A-Z0-9_]*TOKEN=(?!\{\{)[A-Za-z0-9_-]{16,}"), "high"),
    ("Static service token in header", re.compile(r'x-api-token\\?":\s*\\?"(?!\{\{)[A-Za-z0-9_-]{16,}'), "high"),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "critical"),
]

# Field-form collector: secrets assigned to credential-named fields at any
# escape depth. The (?!\{\{) guard skips unresolved template placeholders;
# the letters+digits requirement below skips plain words and slugs.
_FIELD_SECRET = re.compile(
    r'(?:api_?key|secret|password|access_token|auth_token|client_secret)'
    r'\\*"?\s*[:=]\s*\\*"?(?!\{\{)([A-Za-z0-9_.\-]{20,})', re.I)


def collect_secret_values(text):
    values = set()
    for _kind, pat, _sev in SECRET_PATTERNS:
        for m in pat.finditer(text):
            v = m.group(m.lastindex or 0)
            v = re.sub(r"^[^A-Za-z0-9]+", "", v).rstrip('\\"')
            if len(v) >= 16:
                values.add(v)
    for m in _FIELD_SECRET.finditer(text):
        v = m.group(1).rstrip('\\"')
        if len(v) >= 20 and re.search(r"[A-Za-z]", v) and re.search(r"[0-9]", v):
            values.add(v)
    return sorted(values, key=len, reverse=True)


def scan_security(text):
    """Aggregated, human-readable findings. Never emits the values."""
    findings = []
    for kind, pat, sev in SECRET_PATTERNS:
        n = len(pat.findall(text))
        if n:
            findings.append({
                "kind": kind, "where": "project export", "count": n, "severity": sev,
                "detail": f"A value matching the {kind} pattern appears {n} time(s) in the "
                          "exported step inputs/outputs. Rotate the credential and mask "
                          "resolved secrets before they reach the export.",
            })
    return findings


def redact_text(text):
    values = collect_secret_values(text)
    n = 0
    for v in values:
        if v in text:
            n += text.count(v)
            text = text.replace(v, v[:6] + "***REDACTED***")
    return text, n, len(values)


# --------------------------------------------------------------------------- #
# Tree normalization (v3 list-based export shape; tolerates the older
# dict-based shape too).
# --------------------------------------------------------------------------- #

def steps_by_name(node):
    raw = node.get("steps")
    if isinstance(raw, dict):
        return dict(raw), list(raw.keys())
    out, order = {}, []
    for s in _as_list(raw):
        if isinstance(s, dict) and s.get("name"):
            out[s["name"]] = s
            order.append(s["name"])
    return out, order


def step_outputs(step):
    """A step's output wrapper {'output': ..., 'completion_time': ...}."""
    if not isinstance(step, dict):
        return {}
    out = step.get("output")
    if out is None:
        out = step.get("outputs")
    return out if isinstance(out, dict) else {}


def step_result(step):
    """The primary payload: output.output, unwrapping {'results': {...}}."""
    inner = step_outputs(step).get("output")
    if isinstance(inner, dict) and isinstance(inner.get("results"), dict):
        return inner["results"]
    return inner


def node_output_result(node, step_name):
    """Same unwrap, but via the project node's own output dict."""
    entry = _as_dict(_as_dict(node.get("output")).get(step_name))
    inner = entry.get("output")
    if isinstance(inner, dict) and isinstance(inner.get("results"), dict):
        return inner["results"]
    return inner


# --------------------------------------------------------------------------- #
# Transcript helpers - message format is content[] parts with
# type == 'tool-call' (toolName) / 'tool-result' / 'text' / 'reasoning'.
# --------------------------------------------------------------------------- #

def tool_counts(messages):
    counts = {}
    for m in _as_list(messages):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "tool-call":
                    name = part.get("toolName") or "unknown"
                    counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def find_final_answers(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "final_answer" and isinstance(v, str):
                acc.append(v)
            else:
                find_final_answers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            find_final_answers(v, acc)


def extract_messages(child_steps):
    """Full transcript: send_agent_logs request_params.messages, falling back
    to get_agent_logs output.response.messages. Returns the longer list."""
    sal = child_steps.get("send_agent_logs") or {}
    a = _as_list(_as_dict(_as_dict(sal.get("inputs")).get("request_params")).get("messages"))
    gal = child_steps.get("get_agent_logs") or {}
    inner = step_outputs(gal).get("output")
    b = _as_list(_as_dict(_as_dict(inner).get("response")).get("messages")) if isinstance(inner, dict) else []
    return a if len(a) >= len(b) else b


def extract_final_answer(child_steps):
    fa = child_steps.get("parse_agent_final_answer") or {}
    inner = step_outputs(fa).get("output")
    if isinstance(inner, list):
        strings = [s for s in inner if isinstance(s, str)]
        if strings:
            return max(strings, key=len)
    if isinstance(inner, str) and inner.strip():
        return inner
    acc = []
    find_final_answers(step_outputs(fa), acc)
    return max(acc, key=len) if acc else None


# --------------------------------------------------------------------------- #
# Workfile stitching - ported from the toolkit: rebuild shared files from the
# agents' own cat/sed/grep reads captured in transcripts. Only simple
# single-read commands count; sed output is clipped to its range; grep -n
# line numbers take priority; tool results are paired by toolCallId.
# --------------------------------------------------------------------------- #

_SIMPLE_READ = re.compile(r"^\s*(?:cd [^;&|]+[;&]{1,2}\s*)?(cat|sed -n|grep -n)")
_READ_PREFIX = r"^\s*(?:cd [\w./~-]+\s*(?:&&|;)\s*)?"


def _result_text(part):
    out = part.get("output", part.get("result"))
    if isinstance(out, dict):
        v = out.get("value", out)
        return json.dumps(v) if isinstance(v, dict) else str(v)
    if isinstance(out, list):
        return " ".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in out)
    return str(out)


def _clean_result(t):
    if "\\n" in t and t.count("\\n") > t.count("\n"):
        t = t.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")
    cut = t.find("[... output truncated")
    return t[:cut] if cut >= 0 else t


def _short_name(path):
    if "/artifacts/" in path:
        tail = path.split("/artifacts/", 1)[1]
        parts = tail.split("/", 1)
        return parts[1] if len(parts) == 2 else tail
    return path


_HEREDOC_WRITE = re.compile(
    r"(?:cat|tee)\s*>{1,2}\s*([\w./-]+)\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\2",
    re.DOTALL)
_ECHO_WRITE = re.compile(
    r"""(?:echo(?:\s+-[neE]+)?|printf)\s+(?:['\"]%s\\?n?['\"]\s+)?(['\"])(.*?)\1\s*>\s*([\w./-]+)""",
    re.DOTALL)


def stitch_workfiles(transcripts):
    slots = {}
    written = set()   # files whose full content came from a write - kept at any size

    def put(f, n, ln, prio):
        slot = slots.setdefault(f, {})
        if n not in slot or prio > slot[n][0]:
            slot[n] = (prio, ln)

    for tr in transcripts:
        msgs = tr["messages"]
        for i, msg in enumerate(msgs):
            c = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(c, list):
                continue
            for part in c:
                if not (isinstance(part, dict) and part.get("type") == "tool-call"):
                    continue
                inp = part.get("input", part.get("args", {}))
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                if not cmd:
                    continue
                # writes first: heredoc / echo-redirect content is authoritative
                # (the file body is inline in the command itself), works inside
                # compound commands, and beats any read of the same lines.
                for m in _HEREDOC_WRITE.finditer(cmd):
                    f = _short_name(m.group(1))
                    written.add(f)
                    for off, ln in enumerate(m.group(3).split("\n")):
                        put(f, 1 + off, ln, 4)
                for m in _ECHO_WRITE.finditer(cmd):
                    f = _short_name(m.group(3))
                    written.add(f)
                    for off, ln in enumerate(m.group(2).split("\n")):
                        put(f, 1 + off, ln, 4)
                if not _SIMPLE_READ.match(cmd):
                    continue
                if i + 1 >= len(msgs) or not isinstance(msgs[i + 1].get("content"), list):
                    continue
                call_id = part.get("toolCallId") or part.get("id")
                res, fallback = "", ""
                for rp in msgs[i + 1]["content"]:
                    if isinstance(rp, dict) and rp.get("type") == "tool-result":
                        rid = rp.get("toolCallId") or rp.get("tool_call_id") or rp.get("id")
                        if call_id and rid == call_id:
                            res = _clean_result(_result_text(rp))
                            break
                        if not fallback:
                            fallback = _clean_result(_result_text(rp))
                res = res or fallback
                if not res:
                    continue
                gm = re.match(_READ_PREFIX + r"grep -n \"\" ([\w./-]+)(?:\s*\|\s*sed -n '(\d+),(\d+)p')?\s*$", cmd)
                sm = re.match(_READ_PREFIX + r"sed -n '(\d+),(\d+)p' ([\w./-]+)\s*$", cmd)
                cm = re.match(_READ_PREFIX + r"cat ([\w./-]+)(?: 2>(?:/dev/null|&1))?\s*$", cmd)
                if gm:
                    f = _short_name(gm.group(1))
                    for lm in re.finditer(r"(?:^|\n)(\d+):(.*)", res):
                        put(f, int(lm.group(1)), lm.group(2), 3)
                elif sm:
                    a, b, f = int(sm.group(1)), int(sm.group(2)), _short_name(sm.group(3))
                    for off, ln in enumerate(res.split("\n")[: b - a + 1]):
                        put(f, a + off, ln, 2)
                elif cm:
                    f = _short_name(cm.group(1))
                    for off, ln in enumerate(res.split("\n")):
                        put(f, 1 + off, ln, 1)

    out = []
    for f, slot in slots.items():
        if f.startswith("/tmp/") or (len(slot) < 3 and f not in written):
            continue
        ns = sorted(slot)
        parts, prev = [], None
        for n in ns:
            if prev is not None and n > prev + 1:
                parts.append(f"\n... [lines {prev + 1}-{n - 1} not recovered] ...\n")
            parts.append(slot[n][1])
            prev = n
        out.append({
            "name": f, "path": f,
            "note": f"Reconstructed from agent transcript reads: {len(ns)} lines "
                    f"(lines {ns[0]}-{ns[-1]}). Gaps from tool-output limits are marked inline.",
            "text": "\n".join(parts),
        })
    return out


# --------------------------------------------------------------------------- #
# Concept pulls - ported from the toolkit: Concept/Doctrine/LegalArgument
# node pulls found in transcript tool calls and results.
# --------------------------------------------------------------------------- #

_CONCEPT_TYPES = ("Concept", "Doctrine", "LegalArgument")


def extract_concept_pulls(transcripts):
    by_agent = {}
    for tr in transcripts:
        queries, nodes = [], {}
        for i, msg in enumerate(tr["messages"]):
            c = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(c, list):
                continue
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool-call":
                    inp = part.get("input", part.get("args", {}))
                    if not isinstance(inp, dict):
                        continue
                    typ = str(inp.get("type", inp.get("node_type", "")))
                    if any(t in typ for t in _CONCEPT_TYPES):
                        queries.append({"msg": i, "tool": part.get("toolName", ""),
                                        "q": str(inp.get("q", inp.get("ref_id", "")))[:200],
                                        "type": typ, "namespace": inp.get("namespace", "(none)")})
                elif part.get("type") == "tool-result":
                    blob = json.dumps(part.get("output", ""))
                    for m in re.finditer(
                            r'name\\?":\\?"([^"\\\\]+)\\?",\\?"node_type\\?":\\?"(Concept|Doctrine|LegalArgument)', blob):
                        nm, nt = m.groups()
                        nodes.setdefault(nm, {"name": nm, "node_type": nt, "first_msg": i})
                    for m in re.finditer(
                            r'node_type\\?":\\?"(Concept|Doctrine|LegalArgument)\\?",.{0,200}?name\\?":\\?"([^"\\\\]+)', blob):
                        nt, nm = m.groups()
                        nodes.setdefault(nm, {"name": nm, "node_type": nt, "first_msg": i})
        by_agent[tr["name"]] = {"queries": queries, "nodes": list(nodes.values())}
    return by_agent


# --------------------------------------------------------------------------- #
# Documents - built from the doc-ingest children's parse_document elements.
# plain + html are internal handoff fields; run-report-build.py lifts html
# into source_docs [{id, title, html}] and strips both from the published
# documents[] metadata.
# --------------------------------------------------------------------------- #

def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_documents(doc_children):
    docs, seen_ids = [], set()
    for child in doc_children:
        csteps, _ = steps_by_name(child)
        sv = step_result(csteps.get("set_var", {})) or {}
        sv = sv if isinstance(sv, dict) else {}
        file_url = sv.get("file_url") or ""
        fname = file_url.rsplit("/", 1)[-1].split("?")[0] if file_url else str(child.get("project_id"))
        # graph_title carries the run-level task goal on this export - useless
        # as a per-document title, so the filename is authoritative.
        title = fname
        kind = fname.rsplit(".", 1)[-1].lower() if "." in fname else "document"
        strat = step_result(csteps.get("derive_parse_strategy", {})) or {}
        ref = step_result(csteps.get("extract_created_ref_id", {})) or {}

        pd_res = step_result(csteps.get("parse_document", {})) or {}
        els = parse_maybe_json(pd_res.get("elements_json")) if isinstance(pd_res, dict) else None
        plain_lines, html_parts = [], []
        for el in _as_list(els):
            if not isinstance(el, dict):
                continue
            text = (el.get("text") or "").strip()
            if not text:
                continue
            plain_lines.append(text)
            etype = el.get("type") or ""
            if etype in ("Title", "Header"):
                html_parts.append(f"<h3>{_esc(text)}</h3>")
            else:
                html_parts.append(f"<p>{_esc(text)}</p>")

        doc_id = re.sub(r"\.\w+$", "", fname) or str(child.get("project_id"))
        if doc_id in seen_ids:
            doc_id = f"{doc_id}-{str(child.get('project_id'))[-4:]}"
        seen_ids.add(doc_id)
        docs.append({
            "id": doc_id, "file": fname, "title": title, "kind": kind,
            "strategy": strat.get("strategy") if isinstance(strat, dict) else None,
            "ref_id": ref.get("ref_id") if isinstance(ref, dict) else None,
            "already_exists": ref.get("already_exists") if isinstance(ref, dict) else None,
            "start": fmt_ts(iso_to_dt(child.get("created_at"))),
            "end": fmt_ts(iso_to_dt(child.get("updated_at"))),
            "plain": "\n".join(plain_lines),   # internal - stripped by build
            "html": "\n".join(html_parts),     # internal - lifted into source_docs
        })
    return docs


# --------------------------------------------------------------------------- #
# Final deliverable - fetched from the outputs map and converted with stdlib
# only (a .docx is a zip containing word/document.xml). Failures degrade to a
# warning; the report still builds.
# --------------------------------------------------------------------------- #

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MAX_DELIVERABLE_BYTES = 25 * 1024 * 1024


def _docx_xml_to_doc(xml_bytes, fname):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    body = root.find(_W_NS + "body")
    if body is None:
        return None
    plain, html = [], []
    for child in body:
        tag = child.tag
        if tag == _W_NS + "p":
            text = "".join(t.text or "" for t in child.iter(_W_NS + "t")).strip()
            if not text:
                continue
            plain.append(text)
            style_el = child.find(f"{_W_NS}pPr/{_W_NS}pStyle")
            style = (style_el.get(_W_NS + "val") or "").lower() if style_el is not None else ""
            if "title" in style or style in ("heading1", "heading 1"):
                html.append(f"<h2>{_esc(text)}</h2>")
            elif style.startswith("heading"):
                html.append(f"<h3>{_esc(text)}</h3>")
            else:
                html.append(f"<p>{_esc(text)}</p>")
        elif tag == _W_NS + "tbl":
            rows = []
            for ri, tr_el in enumerate(child.findall(_W_NS + "tr")):
                cells = []
                for tc in tr_el.findall(_W_NS + "tc"):
                    cells.append("".join(t.text or "" for t in tc.iter(_W_NS + "t")).strip())
                plain.append(" | ".join(cells))
                tag_c = "th" if ri == 0 else "td"
                rows.append("<tr>" + "".join(f"<{tag_c}>{_esc(c)}</{tag_c}>" for c in cells) + "</tr>")
            html.append('<div class="tblwrap"><table>' + "".join(rows) + "</table></div>")
    return {"plain": "\n".join(plain), "html": "\n".join(html)}


def fetch_deliverables(outputs, warnings):
    import io as _io
    import zipfile
    docs = []
    for fname, url in (outputs or {}).items():
        if not (isinstance(url, str) and url.startswith("http") and fname.lower().endswith(".docx")):
            continue
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                body = resp.read(_MAX_DELIVERABLE_BYTES + 1)
            if len(body) > _MAX_DELIVERABLE_BYTES:
                warnings.append(f"deliverable {fname}: larger than {_MAX_DELIVERABLE_BYTES} bytes; skipped")
                continue
            with zipfile.ZipFile(_io.BytesIO(body)) as zf:
                converted = _docx_xml_to_doc(zf.read("word/document.xml"), fname)
            if not converted or not converted["html"].strip():
                warnings.append(f"deliverable {fname}: no readable content extracted")
                continue
            doc_id = "deliverable-" + re.sub(r"\.\w+$", "", fname)
            docs.append({
                "id": doc_id, "file": fname,
                "title": "FINAL DELIVERABLE - " + fname, "kind": "deliverable",
                "strategy": None, "ref_id": None, "already_exists": None,
                "start": None, "end": None,
                "plain": converted["plain"], "html": converted["html"],
            })
        except Exception as exc:  # noqa: BLE001 - never fail the report on a fetch
            warnings.append(f"deliverable {fname}: fetch/convert failed ({exc})")
    return docs


# --------------------------------------------------------------------------- #
# Main parse
# --------------------------------------------------------------------------- #

def parse_export(raw_text):
    warnings = []
    degraded = False

    security = scan_security(raw_text)
    raw_text, masked, distinct = redact_text(raw_text)
    _log(f"redacted {masked} secret-shaped span(s) across {distinct} distinct value(s)")

    try:
        tree = json.loads(raw_text)
    except (ValueError, TypeError) as exc:
        return None, None, None, [f"failed to parse export JSON after redaction: {exc}"]
    if not isinstance(tree, dict):
        return None, None, None, ["export root was not a JSON object"]

    root_steps, step_order = steps_by_name(tree)
    if not root_steps:
        return None, None, None, ["export carried no steps - wrong shape?"]
    root_name = tree.get("name") or ""

    # ---- attach children to their launching root step via child_name suffix
    children_by_alias = {}
    noise_children = 0
    for c in _as_list(tree.get("children")):
        if not isinstance(c, dict):
            continue
        cn = c.get("child_name") or ""
        alias = cn[len(root_name) + 1:] if root_name and cn.startswith(root_name + "_") else None
        noise_children += len(_as_list(c.get("children")))
        if alias and alias in root_steps:
            children_by_alias.setdefault(alias, []).append(c)
        else:
            warnings.append(f"child project {c.get('project_id')} ({cn or c.get('name')}) "
                            "did not match any root step; skipped")

    # ---- config / score / rubrics
    config_src = node_output_result(tree, "set_var") or step_result(root_steps.get("set_var", {}))
    config_src = config_src if isinstance(config_src, dict) else {}
    if not config_src:
        warnings.append("config extraction failed: set_var output missing")
        degraded = True

    run_id = None
    webhook_url = config_src.get("webhook_url")
    if isinstance(webhook_url, str) and webhook_url:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(webhook_url).query)
        run_id = (qs.get("run_id") or qs.get("runId") or [None])[0]
    if not run_id and isinstance(root_name, str) and "-" in root_name:
        tail = root_name.rsplit("-", 1)[1]
        run_id = tail if len(tail) >= 8 else None

    config = {
        "task_slug": config_src.get("task_slug"),
        "task_goal": config_src.get("task_goal"),
        "deliverable": config_src.get("task_output_desc"),
        "run_id": run_id,
        "workspace_id": config_src.get("workspace_id"),
        "graph_base_url": config_src.get("graph_base_url"),
        "models": {k: config_src.get(k) for k in
                   ("model", "checklist_model", "verify_model", "cross_check_model", "judge_model")
                   if config_src.get(k)},
        "flags": {k: config_src.get(k) for k in
                  ("drafters", "use_fanout", "max_iterations", "use_case_law_research",
                   "cross_checker_agent", "agent_ingestion_flag") if config_src.get(k) is not None},
    }

    scores = {}
    sr = node_output_result(tree, "score_rubric") or step_result(root_steps.get("score_rubric", {}))
    if isinstance(sr, dict) and sr.get("scores_json"):
        scores = parse_maybe_json(sr["scores_json"]) or {}
    if not scores:
        warnings.append("score extraction failed: score_rubric scores_json missing/unparseable")
        degraded = True

    rubrics_src = parse_maybe_json(config_src.get("rubrics_json")) or []
    if not rubrics_src:
        attrs = node_output_result(tree, "build_score_rubric_attrs") or {}
        value = attrs.get("value") if isinstance(attrs, dict) else None
        rubrics_src = _as_list(_as_dict(value).get("criteria"))
        if rubrics_src:
            warnings.append("rubrics taken from build_score_rubric_attrs (set_var rubrics_json missing)")
    crit_by_id = {c.get("id"): c for c in _as_list(scores.get("criteria_results")) if isinstance(c, dict)}
    rubrics = [{
        "id": r.get("id"), "title": r.get("title"), "match_criteria": r.get("match_criteria"),
        "verdict": crit_by_id.get(r.get("id"), {}).get("verdict", "?"),
        "reasoning": crit_by_id.get(r.get("id"), {}).get("reasoning", ""),
    } for r in rubrics_src if isinstance(r, dict)]
    if not rubrics:
        warnings.append("rubric extraction produced zero rows")
        degraded = True

    score = {k: scores.get(k) for k in
             ("score", "max_score", "all_pass", "n_criteria", "n_passed", "judge_model", "scored_at")}
    if score.get("n_criteria") is None and rubrics:
        n_passed = sum(1 for r in rubrics if str(r.get("verdict", "")).lower().startswith("pass"))
        score = {"score": n_passed, "max_score": len(rubrics), "all_pass": n_passed == len(rubrics),
                 "n_criteria": len(rubrics), "n_passed": n_passed,
                 "judge_model": config.get("models", {}).get("judge_model"),
                 "scored_at": tree.get("updated_at")}
        warnings.append("score derived from merged rubric verdicts (scores_json absent)")

    # ---- agents + transcripts (children of agent-launching steps)
    transcripts = []
    used_names = set()
    for alias in step_order:
        for child in children_by_alias.get(alias, []):
            csteps, _ = steps_by_name(child)
            msgs = extract_messages(csteps)
            if not msgs:
                continue
            name = re.sub(r"^(run_|write_)", "", alias).replace("_", "-")
            if name in used_names:
                name = f"{name}-{str(child.get('project_id'))[-4:]}"
            used_names.add(name)
            sal_params = _as_dict(_as_dict(_as_dict(csteps.get("send_agent_logs")).get("inputs"))
                                  .get("request_params"))
            cdt, udt = iso_to_dt(child.get("created_at")), iso_to_dt(child.get("updated_at"))
            transcripts.append({
                "name": name,
                "project_id": str(child.get("project_id") or ""),
                "step": alias,
                "agent_label": sal_params.get("agent") or name,
                "start": fmt_ts(cdt), "end": fmt_ts(udt), "duration_s": dur_s(cdt, udt),
                "messages": msgs,
                "final_answer": extract_final_answer(csteps),
                "n_messages": len(msgs),
                "tools": tool_counts(msgs),
            })

    agents_meta = [{k: v for k, v in tr.items() if k != "messages"}
                   | {"truncated": False, "transcript_truncated": False}
                   for tr in transcripts]

    workfiles = stitch_workfiles(transcripts)
    concepts_by_agent = extract_concept_pulls(transcripts)

    # ---- documents (doc-ingest children: fingerprint = parse_document step)
    doc_children = []
    for alias, kids in children_by_alias.items():
        for c in kids:
            csteps, _ = steps_by_name(c)
            if "parse_document" in csteps:
                doc_children.append(c)
    documents = build_documents(doc_children)

    # ---- timeline: root steps in export order; launch steps get child windows
    timeline = []
    for alias in step_order:
        start = end = None
        kids = children_by_alias.get(alias, [])
        if kids:
            starts = [iso_to_dt(c.get("created_at")) for c in kids]
            ends = [iso_to_dt(c.get("updated_at")) for c in kids]
            starts = [d for d in starts if d]
            ends = [d for d in ends if d]
            start = min(starts) if starts else None
            end = max(ends) if ends else None
        timeline.append({"step": alias, "start": fmt_ts(start), "end": fmt_ts(end),
                         "duration_s": dur_s(start, end)})
        # fan-out steps: surface each child project as its own sub-row so the
        # parallel work is visible (one row per ingested document, labelled by
        # file and linked to it via doc_id)
        if alias == "foreach_ingest_doc":
            for c in kids:
                csteps, _ = steps_by_name(c)
                sv = step_result(csteps.get("set_var", {})) or {}
                furl = sv.get("file_url", "") if isinstance(sv, dict) else ""
                fname = furl.rsplit("/", 1)[-1].split("?")[0] if furl else str(c.get("project_id"))
                cdt, udt = iso_to_dt(c.get("created_at")), iso_to_dt(c.get("updated_at"))
                timeline.append({"step": f"ingest: {fname}", "doc_id": re.sub(r"\.\w+$", "", fname),
                                 "start": fmt_ts(cdt), "end": fmt_ts(udt), "duration_s": dur_s(cdt, udt)})

    # ---- branches as plain strings (Hive narrows to string[])
    branches = []
    for alias in step_order:
        inp = _as_dict(root_steps[alias].get("inputs"))
        if "else_statement" in inp or "condition_groups" in inp:
            branches.append(f"{alias} - then: {inp.get('statement') or '(next step)'}, "
                            f"else: {inp.get('else_statement') or '(next step)'}")
            if inp.get("else_statement") == "system.succeed":
                branches.append(f"NOTE: {alias} reports success on its else branch - "
                                "a failing run ends as workflow success.")

    # ---- outputs: the deliverable map only
    outputs = {}
    ao = node_output_result(tree, "assemble_output_map") or step_result(root_steps.get("assemble_output_map", {}))
    if isinstance(ao, dict):
        outputs = parse_maybe_json(ao.get("outputs_json")) or {}
    if not isinstance(outputs, dict):
        outputs = {}
    documents.extend(fetch_deliverables(outputs, warnings))

    # deterministic checklist: the workflow itself carries the full checklist
    # content; add it as a workfile when the stitcher did not recover one.
    if not any(w["path"].endswith("checklist.md") for w in workfiles):
        cc = node_output_result(tree, "set_checklist_content") or \
             node_output_result(tree, "parse_checklist")
        if isinstance(cc, dict):
            cc = cc.get("checklist_content") or cc.get("checklist")
        if isinstance(cc, str) and cc.strip():
            workfiles.insert(0, {
                "name": "checklist.md", "path": "checklist.md",
                "note": "Content taken from the workflow's own checklist step output "
                        "(set_checklist_content/parse_checklist).",
                "text": cc,
            })

    # ---- health notes / stats
    root_cdt, root_udt = iso_to_dt(tree.get("created_at")), iso_to_dt(tree.get("updated_at"))
    wall_min = round(((root_udt - root_cdt).total_seconds() / 60)) if root_cdt and root_udt else None
    health_notes = [
        "Transcripts came from the structured project export: no log-line truncation.",
        f"{noise_children} polling/progress child project(s) were counted and excluded from the timeline.",
    ]
    if masked:
        health_notes.append(f"{masked} secret-shaped span(s) redacted before parsing "
                            f"({distinct} distinct value(s)); see the security section.")

    n_steps_total = len(root_steps) + sum(len(_as_list(c.get("steps")))
                                          for c in _as_list(tree.get("children")) if isinstance(c, dict))
    page_data = {
        "config": config,
        "score": score,
        "rubrics": rubrics,
        "timeline": timeline,
        "agents": agents_meta,
        "documents": documents,
        "branches": branches,
        "health_notes": health_notes,
        "wall_clock_min": wall_min,
        "log_stats": {"total_lines": n_steps_total, "untagged_lines": 0,
                      "projects": 1 + len(_as_list(tree.get("children"))) + noise_children,
                      "noise_projects": noise_children,
                      "transcripts_truncated": 0, "n_transcripts": len(transcripts)},
        "security": security,
        "outputs": outputs,
        "workfiles": workfiles,   # internal handoff - relocated by run-report-build.py
        "degraded": degraded,
    }
    concepts = {
        "by_agent": concepts_by_agent,
        "concept_pulls": [{"agent": n, "project_id": next((t["project_id"] for t in transcripts
                                                           if t["name"] == n), None), **v}
                          for n, v in concepts_by_agent.items()],
    }
    return page_data, transcripts, concepts, warnings


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

_download_url = _get("download_url")
_top_warnings = []
_raw = ""
if isinstance(_download_url, str) and _download_url.strip():
    try:
        with urllib.request.urlopen(_download_url.strip(), timeout=120) as resp:
            _body = resp.read()
        try:
            _raw = gzip.decompress(_body).decode("utf-8", errors="replace")
        except OSError:
            _raw = _body.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        _top_warnings.append(f"failed to fetch/decompress download_url: {exc}")
else:
    _top_warnings.append("download_url missing or empty")

if _raw:
    _pd, _tr, _co, _pw = parse_export(_raw)
else:
    _pd, _tr, _co, _pw = None, None, None, []

if _pd is None:
    _pd = {"config": {}, "score": {}, "rubrics": [], "timeline": [], "agents": [],
           "documents": [], "branches": [], "health_notes": [], "wall_clock_min": None,
           "log_stats": {}, "security": [], "outputs": {}, "workfiles": [], "degraded": True}
    _tr, _co = [], {"by_agent": {}, "concept_pulls": []}

_all_warnings = _top_warnings + (_pw or [])
_log(f"agents={len(_tr)} rubrics={len(_pd['rubrics'])} timeline={len(_pd['timeline'])} "
     f"workfiles={len(_pd['workfiles'])} documents={len(_pd['documents'])} warnings={len(_all_warnings)}")

print(f"page_data_json: {json.dumps(_pd, default=str)}")
print(f"transcripts_json: {json.dumps(_tr, default=str)}")
print(f"concepts_json: {json.dumps(_co, default=str)}")
print(f"warnings: {json.dumps(_all_warnings, default=str)}")
