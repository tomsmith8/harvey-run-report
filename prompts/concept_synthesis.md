# Concept-usage synthesis

You are synthesizing per-agent audits of Concept-registry usage across one
pipeline run. Use simple technical English; keep node names, numbers, and quotes
verbatim.

Run context:
{RUN_CONTEXT}

Per-agent audit results (JSON):
{PER_AGENT_AUDITS}

Produce:
1. overall_narrative - did the registry help this run? Where did it help and where
   was it unused or harmful?
2. concept_matrix - one row per distinct concept pulled by any agent (dedupe by
   name; fold broad neighbor-expansion noise into a single row), with the agents
   that pulled it, a run-level verdict, and a one-sentence note. Most consequential
   first, max 25 rows.
3. relation_to_failures - for each failed rubric in the run context: did any pulled
   or pullable concept bear on it? Quote what the agents actually saw.
4. recommendations - concrete changes to concept content, retrieval prompts, or
   which agents should pull what (max 6).
