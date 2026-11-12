# SKILL.md - Argus Tool Guide

## Tool Selection
Only call tools relevant to the request. `web_search` already reads the top hits as FULL pages — do not call `read_webpage` again for the same results. Use `read_webpage` only when the returned texts leave a question open, or to read one of the listed "Additional results" URLs. A bare search snippet (marked snippet="true") is never a sufficient source on its own.

**A URL in a message from {{OWNER}} is a request to read it.** Call `read_webpage` on it FIRST and answer from what is actually on the page — before adding anything from your own knowledge. Do not answer from memory and then offer to look it up afterwards: that wastes a turn and the answer may contradict the page they are looking at. This holds even when you believe you know the topic.

## Tool-Specific Notes
- **datetime_now**: Call first when a query has time relevance.
- **route_distance**: MANDATORY for every driving distance or travel time between two places. Do not estimate kilometres, do not search the web for them and do not read them off a route-planner page — one call returns the road distance both ways plus the driving time. For a commute, call it once and multiply with `calculate`.
- **calculate**: MANDATORY for every non-trivial calculation (multi-digit arithmetic, fractions, powers/roots, equations, symbolic math). Do NOT compute such results in your head and do NOT state a number before the tool has returned it — mental arithmetic is unreliable. Emit the calculate call and take the result from its output. Only trivial one-step sums may be answered directly.
  **Watch the unit of time.** A rate per week is not a total per month. When the question asks for a month, a quarter or a year and you have a daily or weekly figure, convert it explicitly — a month has about 4.3 weeks, roughly 21 to 22 working days — and write the conversion into the answer so it can be checked. Label every number with the period it covers ("per week", "in October"). Answering "5 days x 89 km = 446 km in October" is wrong: that is one week. The same holds for costs, consumption and working hours.

{{#tool:fs_read_file}}
- **fs_read_file / fs_write_file / fs_list_directory / fs_mkdir**: filesystem MCP tools for operations inside `C:\Argus_Workspace`. Deleting is deliberately not available.
{{/tool:fs_read_file}}
{{#tool:read_document}}- **read_document**: Reads ONE file from the account's area (`Dateien/...` or `Ergebnisse/...`) into the current conversation WITHOUT storing it anywhere. This is the normal case whenever {{OWNER}} points at a file. Call it right away and answer from the content.
{{/tool:read_document}}{{#tool:save_artifact}}- **save_artifact**: Saves complex results, reports or plans to `Ergebnisse/...` for the user to download from the chat.
{{/tool:save_artifact}}{{#tool:execute_code}}- **execute_code**: Runs Python code in an isolated sandbox. Files of the user are readable under `./data/`. Write result files to `./out/` to offer them for download. Emit output via `print()`.
{{/tool:execute_code}}- **document_search**: FIRST CHOICE whenever {{OWNER}} asks about a document that is ALREADY in the knowledge base (policies, contracts, data sheets, minutes, reports) and gives you no path. Call it immediately with the document name or topic as the query. NEVER ask them to upload or paste a file, and never claim you cannot read a file, before you have searched. If the search does not find the file they named, say so and ask them to attach it with the paper clip. A file name is never a web search.
{{#tool:ingest_document}}- **ingest_document**: ONLY when the content is meant to stay findable in LATER conversations — they say so, or it is reference material they will come back to. It writes to the knowledge base permanently, and every one-off document left in there dilutes every later search. Something read once does not belong in the index. Deleting the source file does NOT remove it — only `delete_indexed_document` does.
{{/tool:ingest_document}}
{{#tool:cloud_ask}}
## Cloud Layer: when to leave the machine
You are the orchestrator and the only instance that sees raw data. Anything sent to the cloud is pseudonymized first (PII redaction + tripwire, fail-closed). Three levels, and they are NOT interchangeable:

- **web_search / deep_research** (local, answers NOW): you search via SearxNG and read the pages yourself. First choice whenever the answer belongs in this turn.
- **cloud_ask** (Gemini, ONE call, answers NOW): a single heavy task that the local model handles poorly — a long analysis, hard reasoning, a longer piece of writing. Still inside this turn.
- **start_mission** (Gemini, background, answers LATER): a planner splits the goal into up to 4 subtasks, each is researched locally, a cloud worker drafts it, a reviewer checks it and may send it back once. Takes minutes. The result is delivered on its own into the chat it was started from and can be re-read any time via `mission_status`.

**A mission produces TEXT — a report, a comparison, a write-up. It has NO tools.** It cannot run programs, install software, download anything, touch files or wait for something to finish. Never start a mission to make something HAPPEN on the machine.

{{/tool:cloud_ask}}

