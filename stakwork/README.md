# Stakwork Script-skill ports

Standalone, stdlib-only versions of the pipeline for the Stakwork workflow
(the Script skill runs ONE file, no filesystem, no cross-file imports;
inputs arrive as injected globals; outputs are `key: value` stdout lines).

| file | workflow step | inputs | outputs |
|---|---|---|---|
| run-report-parse-project.py | after poll_export | `download_url` | `page_data_json`, `transcripts_json`, `concepts_json`, `warnings` |
| run-report-assemble-prompt-inputs.py | before the LLM fan-out | `page_data_json`, `concepts_json`, `transcripts_json` | `failed_rubrics_block`, `agents_items`, `run_context`, `stations`, counts, `warnings` |
| run-report-build.py | the join, before S3 upload | `page_data_json`, `run_llm`, four phase vars | `report_data`, `warnings`, `llm`, `ok`, `analysis_mode`, `degraded` |

## The flow, end to end

```
GET project export ──> parse ──> assemble ──> LLM fan-out ──> build (join) ──> S3 ──> hive report_url
                         │                                      ▲
                         └── run_llm=false skips the middle ────┘   (deterministic mode)
```

1. **Fetch.** The workflow GETs the project-tree endpoint for the run and lands
   the JSON at a URL (json_to_file / S3). That URL is the only input downstream.

2. **Parse** (`run-report-parse-project.py`, input `download_url`). Downloads
   the export, value-redacts secrets, walks the whole child tree (agent runners
   can nest - judge dispute, per-doc ingestion). Emits three JSON vars:
   - `page_data_json` - everything deterministic: config, score, rubric
     verdicts, timeline, agents (including per-doc `kind=ingest` workers),
     documents, workfiles, branches, security, outputs.
   - `transcripts_json` - one entry per agent that has a transcript:
     `{name, project_id, step, agent_label, start, end, duration_s, messages,
     final_answer, n_messages, tools, kind}`.
   - `concepts_json` - concept pulls grouped by agent.

3. **Assemble** (`run-report-assemble-prompt-inputs.py`, inputs = the three
   vars above). Shapes the LLM work-lists so the agent never parses the bundle:
   - `agents_items` - ONE item per transcript agent:
     `{name, step, transcript, final_answer, concept_pulls, truncated}`.
   - `failed_rubrics_block` - ONE item per failed rubric:
     `{rubric_id, rubric_title, match_criteria, judge_reasoning, doc_excerpts,
     workfile_excerpts}`.
   - `run_context`, `stations`, `agent_count`, `failed_rubric_count`, `ok`.
   - Guard the fan-out with `agent_count` / `failed_rubric_count` (an
     IfElseCondition), NOT by testing the arrays.

4. **The agent** (LLM steps; prompt templates live in `../prompts/`). Four
   phases, each phase = fill a template's `{PLACEHOLDER}`s with one item and
   collect the raw-JSON replies into an array:
   - ForEach `agents_items` + `transcript_summary.md` -> collect into
     `transcript_summary_json` (array).
   - ForEach `failed_rubrics_block` + `rubric_trace.md` -> collect into
     `rubric_trace_json` (array; classification, pathway stations, the four
     ingested/knowable/draft/verify questions, root cause, fixes).
   - ForEach `agents_items` + `concept_audit.md` -> collect into
     `concept_audit_json` (array).
   - ONE call with `run_context` + the audits + `concept_synthesis.md` ->
     `concept_synthesis_json` (object).
   Rules: replies must be raw JSON matching the schema embedded in each prompt
   (no prose, no fences); one failed item is dropped, never fatal - build
   degrades gracefully.

5. **Join / build** (`run-report-build.py`, inputs `page_data_json`,
   `run_llm`, and the four phase vars). Validates and coerces everything to
   the hive contract, derives `rubric_links` (rubric -> source-doc term hits),
   lifts `source_docs`, and emits `report_data` - the single bundle - plus
   `analysis_mode`: `full` (all four phases usable), `degraded` (some phase
   missing/malformed), or `deterministic` (`run_llm=false`, phases skipped).

6. **Publish.** Upload `report_data` to S3 (json_to_file), write that URL to
   hive `stakworkRuns.report_url`. Hive's /report page renders the bundle;
   the hosted `viewer.html?data=<s3-url>` renders the identical bundle
   standalone.

`run_llm_locally.py` is NOT a workflow step: it drives the same three stages
locally against a raw export, using this repo's analyze.py for the LLM phases -
use it to validate changes before touching the workflow. It is the reference
implementation of steps 2-5.

These files are the source of truth; the workflow's Script steps are pasted
copies. When the export shape or the bundle contract changes, fix here first,
run run_llm_locally.py (or report.py --skip-llm) against a saved export, then
re-paste. The consumer contract is hive `src/lib/run-report/types.ts` +
`project.ts`; the bundle must match `build_report.py`'s output shape exactly:
`workfiles[].text`, `rubric_links = {id: [{doc, tokens}]}`, `branches` as
strings, `source_docs = [{id, title, kind, file, html}]`.
