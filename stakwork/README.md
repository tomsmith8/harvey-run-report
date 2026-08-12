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

1. **Fetch.** Live (workflow 58019, steps 1-13): an async export job, not a
   plain GET - set_var literals -> request_export -> guard_export_accepted ->
   poll_export (WhileLoop -> child workflow 58018) -> guard_poll_completed ->
   extract_download_url / take_last_download_url. However it is produced,
   the contract into stage 2 is one string: `download_url`.

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

4. **The agent** (LLM steps). TARGET design - four phases, each phase = fill
   a prompt's `{PLACEHOLDER}`s with ONE item and collect the raw-JSON replies:
   - ForEach `agents_items` + transcript-summary prompt -> collect into
     `transcript_summary_json` (array).
   - ForEach `failed_rubrics_block` + rubric-trace prompt -> collect into
     `rubric_trace_json` (array; classification, pathway stations, the four
     ingested/knowable/draft/verify questions, root cause, fixes).
   - ForEach `agents_items` + concept-audit prompt -> collect into
     `concept_audit_json` (array).
   - ONE call with `run_context` + the collected audits -> 
     `concept_synthesis_json` (object).
   Rules: replies must be raw JSON matching the schema embedded in each prompt
   (no prose, no fences); one failed item is dropped, never fatal - build
   degrades gracefully. Prompt text: the workflow reads Stakwork prompt DB
   records ([[RUN_REPORT_*]] tokens), NOT this repo - `../prompts/*.md` are
   the reference copies to paste into those records.

   ### Live state of workflow 58019 vs this target

   As of Aug 2026 the live stage 4 is the PREVIOUS design: one gate
   (guard_run_llm) and four single-shot chained WorkflowRunner calls (->
   56580), each run ONCE per run, reading the run-wide `agents_block` /
   `agents_concepts_block` outputs of the OLD pasted assemble revision.
   There is no fan-out, no guard_agents_items, no agent_count wiring.

   That old design is the one that produced the 6,224,234-token prompt on
   project 151773880 (HTTP 400, workflow halt): run-wide blocks inline every
   transcript into one prompt. The v3 assemble script in this directory
   deliberately REMOVES those two outputs and emits per-item `agents_items`
   (500k-char cap per agent) instead. Do NOT re-add run-wide blocks - at
   500k chars x N agents a 17-agent run rebuilds the same overflow.

   Migration order for the workflow (paste + rewire together, not
   separately - pasting v3 assemble alone starves the live single-shot
   phases, because `agents_block` references resolve empty):
   1. Paste the v3 run-report-assemble-prompt-inputs.py.
   2. Add guard_agents_items (IfElseCondition on `agent_count` > 0) after
      assemble_prompt_inputs; equivalent guard on `failed_rubric_count`
      before the trace fan-out.
   3. Replace run_transcript_summary with a ForEachCondition over
      `agents_items`, one WorkflowRunner (56580) call per item, per-item
      placeholders (name, step, transcript, final_answer, concept_pulls);
      collect replies into `transcript_summary_json`.
   4. Same ForEach pattern for concept_audit (over `agents_items`) and
      rubric_trace (over `failed_rubrics_block`).
   5. Keep concept_synthesis single-shot, chained on the collected audits.
   6. Update the four [[RUN_REPORT_*]] prompt records from run-wide-block
      placeholders to the per-item placeholders (reference text in
      `../prompts/`).

5. **Join / build** (`run-report-build.py`, inputs `page_data_json`,
   `run_llm`, and the four phase vars). Validates and coerces everything to
   the hive contract, derives `rubric_links` (rubric -> source-doc term hits),
   lifts `source_docs`, and emits `report_data` - the single bundle - plus
   `analysis_mode`: `full` (all four phases usable), `degraded` (some phase
   missing/malformed), or `deterministic` (`run_llm=false`, phases skipped).

6. **Publish.** persist_report_data (JSONToFile -> S3) ->
   build_report_webhook_body -> post_report_url (Request POST to
   `webhook_url`) -> set_output. Hive receives the S3 URL by webhook and
   stores it on stakworkRuns.report_url - the workflow never writes the DB
   directly. Hive's /report page renders the bundle; the hosted
   `viewer.html?data=<s3-url>` renders the identical bundle standalone.

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
