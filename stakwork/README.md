# Stakwork Script-skill ports

Standalone, stdlib-only versions of the pipeline for the Stakwork workflow
(the Script skill runs ONE file, no filesystem, no cross-file imports;
inputs arrive as injected globals; outputs are `key: value` stdout lines).

| file | workflow step | inputs | outputs |
|---|---|---|---|
| run-report-parse-project.py | after poll_export | `download_url` | `page_data_json`, `transcripts_json`, `concepts_json`, `warnings` |
| run-report-assemble-prompt-inputs.py | before the LLM fan-out | `page_data_json`, `concepts_json`, `transcripts_json` | `failed_rubrics_block`, `agents_items`, `run_context`, `stations`, counts, `warnings` |
| run-report-build.py | the join, before S3 upload | `page_data_json`, `run_llm`, four phase vars | `report_data`, `warnings`, `llm`, `ok`, `analysis_mode`, `degraded` |

`run_llm_locally.py` is NOT a workflow step: it drives the same three stages
locally against a raw export, using this repo's analyze.py for the LLM phases -
use it to validate changes before touching the workflow.

These files are the source of truth; the workflow's Script steps are pasted
copies. When the export shape or the bundle contract changes, fix here first,
run run_llm_locally.py (or report.py --skip-llm) against a saved export, then
re-paste. The consumer contract is hive `src/lib/run-report/types.ts` +
`project.ts`; the bundle must match `build_report.py`'s output shape exactly:
`workfiles[].text`, `rubric_links = {id: [{doc, tokens}]}`, `branches` as
strings, `source_docs = [{id, title, kind, file, html}]`.
