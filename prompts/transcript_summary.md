# Transcript summary

You are analyzing the transcript of an AI agent that ran inside a legal-document
benchmark pipeline (Stakwork HARVEY). Produce a faithful structured summary of what
THIS agent did. Use simple technical English: short sentences, active voice, no
metaphors. Keep all numbers, IDs, file names, and tool names verbatim.

Agent under analysis: {AGENT_NAME}
Parent step: {AGENT_STEP}

Run context:
{RUN_CONTEXT}

The transcript below may be tail-truncated (the log pipeline caps line size). The
final answer was recovered separately and covers the truncated tail.

Report faithfully:
- tool calls actually made (count by tool name),
- files read or written (checklist.md, facts.md, spreadsheet.md, work/*, docx artifacts),
- graph queries (which tool, which namespace; flag queries missing the namespace filter),
- what the agent concluded,
- anomalies (wrong-namespace contamination, rate limits, retries, self-corrections,
  contradictions, failed tool calls).

In failed_rubric_relevance, quote exact transcript snippets wherever this agent
touched the topics of the failed rubric criteria listed in the run context. Write
"not touched" when it did not.

## TRANSCRIPT (JSON messages)
{TRANSCRIPT_JSON}

## RECOVERED FINAL ANSWER
{FINAL_ANSWER}
