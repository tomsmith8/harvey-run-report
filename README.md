# HARVEY run report

Turns a HARVEY runner export into two per-run artifacts:

| artifact | what it is | where it goes |
|---|---|---|
| `report-data.json` | the raw JSON struct (schema_version 1) | S3; `report_url` in the hive `stakworkRuns` table |
| `report.html` | baked, self-contained HTML view | hive attachment / anywhere - opens with no hosting |

Plus one repo-level asset, hosted once:

| asset | what it is |
|---|---|
| `viewer.html` | the shared UI. Open as `viewer.html?data=<url-of-report-data.json>`. Updating the hosted viewer re-renders every past run. |

## Usage

```bash
pip install -r requirements.txt

# raw JSON + HTML, no LLM (deterministic, no API key)
python report.py input.json --out out/

# + failure traces (Claude API; needs ANTHROPIC_API_KEY or `ant auth login`)
python report.py input.json --out out/ --llm

# + concept-usage audit (implies --llm)
python report.py input.json --out out/ --concepts
```

The last stdout line is a machine-readable manifest for the calling workflow:

```
MANIFEST {"report_data": "out/report-data.json", "report_html": "out/report.html", "llm": false, "ok": true, "warnings": []}
```

Both artifacts are always produced; `--llm` only changes whether the analysis
sections (failure traces, concept audit) are populated. All outputs are
secret-redacted (`--no-redact` on the parse scripts disables this; unsafe).
Optional stages that fail (doc download, LLM) are reported in `warnings` and the
report still builds.

## Input

`report.py` auto-detects the input shape:

- **Project-tree export** (the GET project-data endpoint): a JSON object
  `{pid, wid, name, status, time_start, time_end, steps: {alias: {skill, status,
  time_start, time_end, inputs, outputs, children?}}, stats}` with epoch-ms
  times and nested child projects. Preferred: transcripts arrive untruncated.
- **Raw CloudWatch log lines** (legacy): a JSON array of
  `{"@timestamp", "@message"}`. Kept for old exports; the parser salvages
  truncated transcripts and stitches working files from read commands.

`.zip` inputs are read directly (first `.json` inside).

## Pipeline stages (individually runnable)

```
parse_project.py | parse_logs.py   input -> page-data.json + extracted/   (redacts secrets)
fetch_docs.py                      downloads + converts source docs and the deliverable
analyze.py                         Claude API analysis -> analysis.json, concepts-analysis.json
build_report.py                    assembles report-data.json + report.html
```

`report.py` chains them; each stage also runs standalone on the same `--out` dir,
which is how you split the LLM phase into separate workflow steps if wanted.

## LLM analysis

`analyze.py` fills the templates in `prompts/` and calls the Claude API with
structured outputs (JSON-schema enforced). Defaults: `claude-opus-5`, effort
`high`, 4 workers, server-side refusal fallback on. Cheaper pass:
`--model claude-sonnet-5 --effort medium`.

Phases: **A** per-agent transcript summaries -> **B** per-failed-rubric traces
(ingested? knowable or derived? did the draft get it? did verification get it?)
-> **C** (`--concepts`) concept-registry usage audit + synthesis.

The prompt files are runner-agnostic templates with `{PLACEHOLDERS}` - they can
run as Stakwork agent steps instead of `analyze.py`. `build_report.py` only
needs `analysis.json` / `concepts-analysis.json` in the shapes defined at the
top of `analyze.py`.

## Downstream workflow (per run)

```
1. GET project data                       -> input.json
2. python report.py input.json --out out --llm
3. upload out/report-data.json to S3      -> the report_url pointer
4. (optional) store out/report.html where hive can serve it
```

One-time: host `viewer.html` (same S3 bucket/CloudFront as the data avoids
CORS entirely). Human link: `viewer.html?data=<encoded report-data.json url>`.

Notes:
- `report-data.json` carries `schema_version`; keep the hosted viewer
  backward-compatible - a breaking viewer change breaks all old runs.
- Bundles contain transcripts and working files (secret-redacted, still
  internal): use private objects + signed URLs or bucket auth.

## Requirements

Python 3.10+. `anthropic` only for `--llm`; `python-docx` + `openpyxl` only for
document conversion. Missing optional deps degrade gracefully.
