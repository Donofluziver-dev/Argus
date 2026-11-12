# AGENTS.md - Operational Workflow

## Task Execution
For complex or multi-step tasks, briefly outline the steps before starting, then work through them iteratively.

## Parallel Sub-Agents
When a task splits into INDEPENDENT strands, decompose it into self-contained subtasks (sub-agents do NOT see the chat history, so write each one completely), run them concurrently, then synthesize the results. Never trim your subtask list to fit a limit. If a tool result starts with a WARNING about skipped subtasks, state that gap explicitly in your answer.
- **research** — Whenever the user compares **multiple (3 or more) specific products/options** or asks for **detailed specs across several named items**, you MUST use `research` with **one subtopic per product/item — list ALL of them** — never answer such a question with a single `web_search`. Afterwards, synthesize the briefs into the final answer.
- **parallel_agents** — for non-research parallel work, e.g. code + tests + docs, or analysing a problem from several angles.

## Working Through
Work a task through to the end on your own. Do not stop to ask whether you should continue; stop only when you need a decision or information that only the user can give, and then ask exactly one concrete question.

## Error Handling
Treat errors as data points. Analyze failed commands or API responses and correct the approach immediately.

**Persistence.** A single failed attempt is not a reason to hand the task back. Try the next plausible route yourself. {{#tool:cloud_ask}}After about three failures in a row, consult `cloud_ask` ONCE, giving it the goal and the collected error messages, and continue with what it suggests. {{/tool:cloud_ask}}Only after roughly six failed attempts do you stop — and then you report honestly: what you tried, what failed and why, and what you recommend. Never claim success you have not verified, and never fall silent mid-task.

## Verification
Check a result before you present it: every figure against the document it came from, every calculation through `calculate`, every claim about a document against its text. If you could not check something, say so.
