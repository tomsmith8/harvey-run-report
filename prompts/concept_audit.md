# Concept-pull audit (per agent)

You are auditing how ONE agent used the knowledge-graph Concept registry -
reusable knowledge nodes (node_type Concept, plus Doctrine/LegalArgument) holding
document-type templates, drafting tips, and practice-area doctrine. Retrieval is
not application: for every pull, check whether the content changed the agent's
output. Use simple technical English; keep node names and quotes verbatim.

Agent under audit: {AGENT_NAME}

Deterministic extraction of this agent's concept queries and retrieved nodes
(ground truth for WHAT was pulled; your job is HOW and WHETHER IT MATTERED):
{CONCEPT_PULLS}

For every concept node (group duplicates): how it was retrieved (tool + query;
note if it arrived as noise from a broad neighbor expansion), what its content
said (quote the tool result), and whether the agent APPLIED it downstream (point
at concrete influence in the transcript or final answer). Verdicts:
effective | partially-used | ignored | irrelevant-noise.

If this agent pulled ZERO concepts: establish why from its prompts (was the
registry mentioned? did it use other tools instead?) and whether that hurt.

Run context (for missed_opportunities):
{RUN_CONTEXT}

## TRANSCRIPT (JSON messages)
{TRANSCRIPT_JSON}

## RECOVERED FINAL ANSWER
{FINAL_ANSWER}
