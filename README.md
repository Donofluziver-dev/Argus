# Argus

*English · [Deutsch](README.de.md)*

A personal AI assistant that runs entirely on your own machine. The language model sits
on your GPU, your documents in your own database, web search goes through your own search
engine. No vendor watches, no subscription, nothing gets handed over — unless you
deliberately switch on the cloud stage, and then it is pseudonymised first.

Argus is not a chat window with search bolted on. It reads documents, researches the web,
computes in a sandbox, speaks and listens, and it **operates the Windows machine** it runs
on — through its own user account, over SSH, with a confirmation before every write.

> **A desktop app is on the way.** Argus is moving out of the browser: chat, deep research,
> long-term memory, your own documents and a new coding agent in one Windows app, running on
> a larger local model. It is in active development and has no release date yet.
> [What's coming](#whats-next)

**Contents:** [What it does](#what-it-does) · [Who it is for](#who-it-is-for) · [Architecture](#architecture) · [Measurements](#measurements) · [Requirements](#requirements) · [Setup](#setup) · [Configuration](#configuration) · [Usage](#usage) · [Operations](#operations) · [Security](#security) · [Limitations](#limitations) · [What's next](#whats-next)

![Monitor view: throughput, KV cache, agent load, tool calls, audit log, eval runs](docs/images/monitor.png)

*The Monitor view (administrators only): 71.3 tokens per second on an RTX 5070 Ti, speech
output on the CPU at a real-time factor of 1.09, and how often the context had to be
compacted. Below: agent load, tool calls, audit log, eval runs and cloud missions.*

![Chat with deep research: five research agents in waves, every search and every page read with its timing](docs/images/research.png)

*A research task in the chat: five research agents in waves, every search and every page
read with its timing, plus the collapsible reasoning and numbered sources.*

![Live view with the orb in its ready state](docs/images/live.png)

*The Live view: hold Space to speak, Esc cancels. The orb shows the state — ready,
listening, thinking, researching, speaking.*

<details>
<summary>Full trace of the same research (50+ steps) and the expanded reasoning</summary>

![Research trace with all searches and pages read](docs/images/research-trace.png)

![Expanded reasoning showing the calculation](docs/images/reasoning.png)
</details>

---

## What it does

**Talk and listen.** Text chat and a voice mode (hold Space to speak). Speech output can
use a cloned reference voice; recognition runs locally on faster-whisper. None of it leaves
the machine.

**Read and judge your own paperwork.** Contracts, invoices, insurance policies, payslips —
PDF, DOCX, EPUB, HTML, plain text. Two separate paths: **read** once (lands only in the
current conversation) or **index** permanently (Qdrant, searchable later). Files are
dragged into the chat.

**Research.** Web search through a self-hosted SearXNG instance, with results read as full
pages rather than snippets. For broader questions, deep research reads several sources in
parallel.

**Operate the computer.** Restart services, check disk space, read event logs, install
Windows updates, run PowerShell. Read-only commands run immediately, writes only after
confirmation, deletions one at a time with a second prompt. The whole thing can be switched
off if you only want the assistant.

**Compute and run code.** Symbolic maths and Python in an isolated sandbox with no network.

**Work out distances.** Driving distance and travel time between two addresses via
OpenStreetMap — one way and round trip, so a commute adds up in a single step. No key
needed; heavy users point `Setup2.py` at their own Nominatim/OSRM.

**Look at images.** Optional — the model is multimodal, but the vision path costs VRAM, so
it sits behind a switch (see [Enabling vision](docs/configuration.md#enabling-vision)).

**Cloud missions.** For tasks beyond the local model there is an optional stage on Gemini:
a planner breaks the goal down, workers research, a reviewer checks. Runs in the
background, the result is delivered to you. Everything that goes out passes a PII detector
first, which blocks when in doubt.

**Remember.** A curated long-term memory per user plus daily notes the assistant draws on
itself.

**Telegram.** Optional access from anywhere, including voice messages, photos and the
confirmation cards for write actions.

---

## Who it is for

Anyone who wants a capable assistant without handing their paperwork to anyone. This is
not a tool for one trade — wherever documents are confidential or a machine needs
operating, there is work for it:

| Who | What Argus does there |
|---|---|
| Private life | compare insurance policies, check a lease, recompute payslips and tax assessments, understand medical letters |
| Trades (electrical, plumbing, construction) | make data sheets, standards and manufacturer docs searchable; check quotes and supplier terms; summarise test reports and site measurements |
| Engineering offices | standards and technical documentation as a knowledge base; unit-aware calculations in the sandbox; log files and measurement series evaluated |
| HR | employment contracts, references, works agreements, application files — data that must not leave the building |
| Law firms, medical practices, tax advisers | everything under confidentiality: read files, pull out deadlines and clauses, prepare letters |
| Purchasing, sales, administration | compare quotes, check supply contracts and terms, summarise tenders |
| IT and machine operation | services, updates, event logs, PowerShell — an assistant that is actually allowed to operate the box instead of only suggesting commands |
| Self-employed, clubs and associations | recompute the books, understand contracts and grant notices, research for applications |
| Learning and research | lecture notes and literature made searchable, deep research with sources — and the whole build as an open teaching piece: RAG, agent loop, tool calls, hardening |

It is not meant as a multi-user service for a company. It runs on **one** machine, for its
owner.

---

## Architecture

Ten containers, all bound to `127.0.0.1` only:

| Service | Role |
|---|---|
| `sglang` | language model on the GPU (Qwen3.5-9B, AWQ-quantised) |
| `rag-backend` | core: agent loop, tools, API, user interface |
| `qdrant` | vector store for documents |
| `postgres` | conversations (encrypted), accounts, metadata |
| `searxng` | your own search engine |
| `tts-service` | speech output (Qwen3-TTS on GGML, CPU), optionally with a cloned voice |
| `stt-service` | speech recognition (faster-whisper, CPU) |
| `calc-sandbox` | isolated Python execution, no network |
| `phoenix` | tracing: what the agent did, and when |
| `open-webui` | alternative front end |

The **setup scripts are the source**, not the generated code. `rag_backend/`,
`docker-compose.yml`, `alembic/` and the `.env` are produced by `Setup1-4.py`,
`Setup_Dashboard.py` and `setup_common.py`. A change made directly in a generated file
silently disappears on the next generator run. That is deliberate: what you back up and
hand on is five Python files and one PowerShell script; everything else is reproducible
from them.

---

## Measurements

Two acceptance runs, both against `QuantTrio/Qwen3.5-9B-AWQ` on an RTX 5070 Ti (16 GB):

| Suite | Scope | Result | Date |
|---|---|---|---|
| HumanEval (subset) | 20 of 164 tasks | 20/20 passed, 26 s total | 2026-07-31 |
| Own agent suite | 50 tasks | 50/50 passed, ⌀ 32 s per task | 2026-07-25 |

**Context, so the numbers do not promise more than they hold:** the HumanEval run uses a
subset of 20 tasks, not the full benchmark. The second suite is **home-made**, in the
style of GAIA but not GAIA — it contains categories that do not exist there (system
operation, sandbox, EU law, security checks), and part of the answers are graded by a
model rather than by exact match. It measures whether this assistant solves the tasks it
was built for. It is no use for comparison with published leaderboards.

Task distribution: 20 at level 1, 20 at level 2, 10 at level 3; covering web search,
multi-step reasoning, document search, unit-aware arithmetic, system operation and agent
planning.

---

## Requirements

**Operating system: Windows 11.** Not a preference, a design fact. Machine control talks
PowerShell to the Windows host over SSH, `setup_ssh.ps1` creates the user there, and the
containers mount Windows paths (`C:\Argus_Workspace`). The containers themselves would run
on Linux — but the generators, the paths and the host tools would need adapting. That is
untested.

**GPU.** Developed and measured on an RTX 5070 Ti with 16 GB (Blackwell). The model takes
about 13 GB, the rest is KV cache, i.e. context. The SGLang image is the CUDA 13 variant
(`Dockerfile.blackwell`); older generations (Ada, Ampere) need a different base image and
are untested. More VRAM converts directly into context and image handling, see
[Configuration](#configuration).

**RAM.** Developed on 64 GB. Speech output, speech recognition, embeddings and the reranker
deliberately run on the CPU so the GPU belongs to the language model. The memory limits of
those containers add up to roughly 26 GB (TTS 12, backend 8, STT 3, Phoenix 2, sandbox
0.5), plus SGLang itself. I would not attempt it below 32 GB.

**Disk.** Around 60 GB for images and model weights.

**Docker Desktop** with the WSL2 backend and GPU access (`nvidia-smi` must work inside the
WSL distribution).

**Python 3.12+ on the host**, only for the generators. Three packages:

```bash
pip install cryptography PyJWT python-dotenv
```

Everything else lives in the containers.

**Accounts and tokens.** A HuggingFace token is required (model downloads). Gemini key and
Telegram bot are optional — empty means off.

---

## Setup

As Administrator, in the project folder:

```powershell
.\setup_ssh.ps1
```

Creates the local user `argus`, installs OpenSSH, generates the Ed25519 key pair, sets the
firewall rule (port 22 only from the Docker networks) and the working directory.

Then enter the tokens (details under [Keys and secrets](docs/configuration.md#keys-and-secrets--api_tokens)):

- `API_Tokens/HF_TOKEN.txt` — required
- `API_Tokens/Gemini.txt`, `API_Tokens/telegram.txt` — optional, empty means off

Generators in this order (`Setup1.py` pulls in `Setup_Dashboard.py` automatically):

```bash
python Setup1.py && python Setup2.py && python Setup3.py && python Setup4.py
```

Build and start — the first run downloads the model weights, which takes a while:

```bash
docker compose up -d --build
```

Then create the first accounts. The **first** account becomes administrator in each case:

- http://127.0.0.1:7860/dashboard/ — Argus chat
- http://localhost:3000 — Open WebUI (its own account management)

On the first run of `Setup2.py` Argus writes its personality to
`C:\Argus_Workspace\identity\SOUL.md` — tone, rules, and how it addresses you. Enter your
name beforehand: either `"owner_name": "Your Name"` in `LLM_CONFIG` (`Setup2.py`) or, if you
pass your setup files on, as a line `OWNER_NAME=Your Name` in the `.env` — the generators
leave it in place. Empty means Argus addresses you without a name. The identity files are
meant to be edited and are read live; a generator run does not overwrite them.

---

## Configuration

All switches live in Python dicts inside the setup scripts, **not in the `.env`**. The
`.env` is generated from them and overwritten on the next run. The path is always the
same: change the value in the setup script, run the script, restart the affected
containers.

| Area | Where | Afterwards |
|---|---|---|
| Language model, context, vision, RAG, research | `Setup2.py` → `LLM_CONFIG` | `python Setup2.py`, then `docker compose up -d sglang rag-backend` |
| Machine control, SSH, approvals | `Setup3.py` → `ACTION_CONFIG` | `python Setup3.py`, then `docker compose up -d rag-backend` |
| Cloud missions (Gemini) | `Setup4.py` → `write_env_block("SETUP4", …)` | `python Setup4.py`, then `docker compose up -d rag-backend` |
| Voice, rate limits, registration, uploads, tracing | `Setup1.py` → `env` | `python Setup1.py`, then `docker compose up -d` |

The generator only writes a value that has changed; a run without changes is a plain
pass-through.

The individual areas are covered in the [configuration reference](docs/configuration.md):
keys and secrets, Telegram, the Gemini cloud stage, switching off machine control, vision,
context size, swapping the model, voice and all further switches.

---

## Usage

| Interface | Address |
|---|---|
| Argus chat | http://127.0.0.1:7860/dashboard/ |
| Open WebUI | http://localhost:3000 |
| Tracing | http://127.0.0.1:6006 |
| Telegram | if configured |

The Argus chat has three views: **Live** (voice mode, hold Space to speak, Esc cancels),
**Chat** (history; files are dragged in) and **Monitor** (administrators only: overview,
tracing, logs, accounts, collection).

Two roles. `admin` may do everything, `chat` may only talk and write. Self-registered
users always get `chat`.

Capabilities are invoked in plain language, not through commands:

| Intent | Example |
|---|---|
| Check paperwork | drag the file into the chat, then "is this contract okay?" |
| Research | "research the current state of …" |
| Windows action | "how much space is left on C:?", "restart the Spooler service" |
| Index permanently | "ingest the file C:\Users\…\report.pdf" |
| Cloud mission | "create a mission: compare … and summarise" |

Files dragged into the chat land in `C:\Argus_Workspace\Uploads` and are removed
automatically after 24 hours (`UPLOAD_RETENTION_HOURS`, `0` disables it). Whatever should
stay findable belongs in the knowledge base via indexing.

---

## Operations

| Purpose | Command |
|---|---|
| Rebuild after a code/setup change | `docker compose up -d --build` |
| Re-read configuration only | `docker compose up -d` |
| Follow the log | `docker compose logs -f rag-backend` |
| Stop | `docker compose down` |
| Full reset — **deletes database and Qdrant** | `docker compose down -v` |
| Tests | `docker compose run --rm --no-deps -v ${PWD}/rag_backend/tests:/app/rag_backend/tests rag-backend python -m pytest rag_backend/tests -q` |

Index documents in bulk: put files into `documents_to_ingest/`, then

```bash
docker compose exec rag-backend python -m rag_backend.ingest
```

After any change, always run the generators in their order first, then `docker compose`.
Never the other way round.

Back up: the setup files, `API_Tokens/` (contains the private SSH key and the Fernet key),
`assets/` (fonts, binary) and — if you use your own voice — `voice_ref/`. The two folders
holding secrets belong in no repository.

---

## Security

The assistant is allowed to operate the machine. That is the point where a system like
this becomes dangerous, so here is an honest account of what the hardening does and what
it does not.

### The model gets nothing past the gate

Every PowerShell script the agent wants to run is classified before execution — on the
text after comments, string literals, escapes and variables have been stripped, so that
nothing can hide behind them:

| Level | Examples | What happens |
|---|---|---|
| **READ** | `Get-Process`, `Get-Service`, `Test-Path`, `docker ps` | runs immediately |
| **WRITE** | `Set-*`, `New-*`, `Copy-Item`, redirections `>`, assignments, unknown cmdlets | confirm once per task; the approval covers the further steps of the same task for 30 minutes |
| **DESTRUCTIVE** | `Remove-*`, `del`, `.Delete()`, `Invoke-Expression`, `& $variable`, `Add-Type`, `New-Object` | confirm every single time, never through a task approval |
| **BLOCKED** | `Format-Volume`, `Stop-Computer`, `Set-ExecutionPolicy`, access to `API_Tokens`, `.ssh`, `.pem`, `.kdbx`, `.env` | never executed, not even with confirmation |

On top of that, a **veto against native binaries** that applies to every level: `cmd`,
`powershell`, `pwsh`, `reg`, `sc`, `schtasks`, `certutil` and any path to `.exe`/`.bat`/`.ps1`
are rejected, because they could escape the cmdlet-based check. Reading file **contents**
outside `C:\Argus_Workspace` (`Get-Content`, `Select-String`, `Import-Csv`) needs a
confirmation — listing and system state do not. The gate is **fail-closed**: whatever it
cannot clearly recognise as read-only counts as a write.

A rejection ends the task. The approval dialog expires after 120 seconds, and a rejection
is followed by a 30-second lock against nagging.

### Access to the host is narrow

- A dedicated local user `argus`. It **is a member of Administrators** — installing
  updates and restarting services does not work without — but it is a separate account
  with its own key, and everything it does goes through the gate and the audit log.
- SSH with an Ed25519 key only; the firewall rule admits port 22 only from the Docker
  networks. The host key is pinned on first connect (TOFU).
- Every session switches PowerShell into Constrained Language Mode. Without WDAC that is
  defence in depth, not a boundary — the boundary is the gate plus confirmation.
- The agent's file tools (`fs_*`) see only `C:\Argus_Workspace`.

### Foreign text is data, not instructions

Web pages, documents read, search results, text in images and tool outputs are framed as
data and HTML-escaped before they enter the context. In deep research a second model call
screens every source for hidden instructions (`web_injection_guard`). Identical tool calls
are deduplicated, and after six consecutive failures the agent stops and reports instead
of trying on.

This does not make the model immune. It bounds what a manipulated model can do: list
directories and query system state without asking — for file contents outside the
workspace and for anything that writes, it has to ask.

![Tracing in the Monitor: span tree of one turn, next to it the tool output wrapped in untrusted_web_content](docs/images/traces.png)

*Verifiable rather than claimed: the tracing view in the Monitor shows every step of a turn
and the raw tool output — here the web page that was read, visibly wrapped as
`<untrusted_web_content>`. That wrapper is what tells the model the text is data, not an
instruction.*

### Only pseudonymised content goes out

Before every cloud call: structured PII by pattern, names by local named-entity
recognition, replaced by placeholders, translated back in the reply. If detection fails,
nothing is sent. A tripwire aborts hard if something like an IBAN or credentials is still
in the package.

### Network, secrets, audit

- All containers are bound to `127.0.0.1`. The Python sandbox has no network at all. Web
  reading rejects private and internal addresses (SSRF filter at DNS level).
- Logins with Argon2 hashes, short-lived tokens, rate limits per endpoint, CSRF check via
  Origin and a host-header check. Chat histories are stored encrypted in Postgres.
- Secrets sit encrypted in `API_Tokens/`, are mounted as `/run/secrets` and decrypted
  in-process. A log filter redacts the Telegram token should a library write it into an
  output after all.
- Container images are pinned by digest, the model to a revision.
- Every system action, every approval and every rejection lands in an HMAC-chained audit
  log that cannot be altered afterwards without notice.

### What this does not protect against

- **Whoever has the machine has everything.** `fernet_key.txt` sits on disk; anyone who
  can read it and `API_Tokens/` can decrypt every secret and every conversation.
- **Not a network service.** There is no TLS, no tenant isolation, only two roles. Opening
  the ports to a network is neither intended nor secured.
- **The gate is a text filter, not a sandbox.** It is built fail-closed and has a test
  suite covering the known bypasses, but it classifies PowerShell by pattern. Hence the
  confirmation — that is the actual boundary.
- **Pseudonymisation is best effort, not perfect.** Named-entity recognition misses names.
  If nothing may leave, leave `Gemini.txt` empty.
- **The model remains a model.** It can be wrong, misread documents and be talked into
  things. Whoever confirms something important should read the command in the card.

Please do not report vulnerabilities as public issues; see [SECURITY.md](SECURITY.md).

---

## Limitations

- One machine, one owner, Windows 11, NVIDIA. No multi-user installation, no Linux host,
  no AMD or Apple GPU.
- 16 GB of VRAM means a 9B model and roughly 20,000 tokens of context. Long documents are
  chunked and read through the index, not in one piece.
- Speech output and recognition run on the CPU — good enough for dialogue, not for long
  read-alouds.
- The measurements above come from a home-made suite. They show that the system solves
  its own tasks, nothing more.

---

## What's next

This README describes the current public release. Alongside it, Argus is becoming a desktop
app, and the browser dashboard will go away in the process. This already runs in the
development version:

- **One app instead of a browser tab.** Chat, deep research, long-term memory and search
  across your own documents move into a Windows app with a regular installer. Voice is still
  to come.
- **Argus Code.** A coding agent that works inside a project folder you trust. It reads,
  searches, edits files and runs commands under approvals you control, and it keeps going
  until the task is finished instead of stopping after every step.
- **A larger local model.** Qwen3.6-35B-A3B on llama.cpp replaces the 9B model, with far more
  context.
- **MCP tools.** MCP servers you set up become tools the agent can use.

There is no date yet: the app ships when it is reliable. Until then, Argus runs the way this
README describes. Watch this repository to hear when it lands.

---

## Third-party components and licences

Argus uses the following projects unmodified as container images or packages; only
`tts-server.cpp` is a patch on an upstream file and carries its notice.

| Component | Role | Licence |
|---|---|---|
| [SGLang](https://github.com/sgl-project/sglang) | model server | Apache-2.0 |
| [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) via [QuantTrio AWQ](https://huggingface.co/QuantTrio/Qwen3.5-9B-AWQ) | language model | Apache-2.0 |
| [Qwen3-TTS](https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF) and [qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp) | speech output | Apache-2.0 / MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | speech recognition | MIT |
| [BAAI bge-m3](https://huggingface.co/BAAI/bge-m3), bge-reranker-v2-m3 | embeddings, reranking | MIT |
| [Qdrant](https://github.com/qdrant/qdrant) | vector database | Apache-2.0 |
| [SearXNG](https://github.com/searxng/searxng) | search engine | AGPL-3.0 (unmodified image) |
| [Open WebUI](https://github.com/open-webui/open-webui) | second front end | own licence (unmodified image) |
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) | tracing | Elastic License 2.0 (unmodified image) |
| LangChain / LangGraph, FastAPI, PostgreSQL, paramiko | backend | MIT / MIT / PostgreSQL / LGPL |
| Barlow, Chakra Petch, JetBrains Mono | UI fonts | SIL OFL (`assets/fonts/`) |

The setup downloads the model weights with your HuggingFace token; the models' licence
terms apply to you.

## Changelog

What changed in each release: [CHANGELOG.md](CHANGELOG.md).

## Licence

See [LICENSE](LICENSE).
