# Failed-rubric root-cause trace

You are doing root-cause analysis for ONE failed rubric criterion in an agent
pipeline run. Trace the rubric pathway: where the expected fact lived, moved, or
was lost between pipeline stations. Use simple technical English. Keep numbers,
IDs, file names, and quotes verbatim. Do not invent evidence: every claim must
point at material in the context below.

FAILED CRITERION {RUBRIC_ID}: {RUBRIC_TITLE}
Match criteria: {MATCH_CRITERIA}
Judge model and FAIL reasoning: {JUDGE_REASONING}

Pipeline stations, in order:
{STATIONS}

Answer the four questions precisely:
1. q_ingested_to_graph - was the expected fact present in the ingested source documents / graph?
2. q_knowable_or_derived - was it stated verbatim in a source (quote it) or only derivable (show the chain)?
3. q_draft_got_it - did the draft contain it, miss it, or deliberately diverge?
4. q_verify_got_it - did any verifier or the aggregator surface it, and what happened downstream?

Then give root_cause (2-5 sentences) and classify:
agent-miss | deliberate-divergence-vs-rubric | rubric-flaw | judge-strictness | data-gap | ingestion-gap.
Be honest about whether this is an agent failure, a benchmark artifact, or judge strictness.
fix_suggestions: concrete changes to prompts, workflow, rubric, or judge that would flip
this to pass or make the disagreement explicit (max 4).

## AGENT SUMMARIES (from the transcript-summary pass)
{AGENT_SUMMARIES}

## SOURCE DOCUMENT EXCERPTS (lines matching this rubric's key terms)
{DOC_EXCERPTS}

## SHARED WORKING FILES (recovered from transcripts)
{WORKFILE_EXCERPTS}
