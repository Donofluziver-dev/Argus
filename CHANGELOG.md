# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/lang/en/).

## [Unreleased]

This repository starts with the edition of Argus for company use. Argus began as a personal
assistant that was distributed as setup scripts and could operate the Windows machine it ran
on. That earlier edition is not part of this repository; the entries below describe how this
edition differs from it. Argus can no longer operate the machine it runs on, and it has two
roles, administrator and user.

### Changed

- **License: Apache 2.0.** `NOTICE` carries the copyright notice, `THIRD_PARTY_NOTICES.md`
  lists the software, fonts and models Argus builds on, with their licenses.
- **Repository layout.** `Setup1.py` to `Setup4.py` and `Setup_Dashboard.py` are gone. Backend,
  browser interface, compose files and services are ordinary files that can be read, reviewed
  and tested. Setup is one script: `python setup/init_stack.py`.
- **Roles are administrator and user.** The role `chat` is now `user`; existing accounts are
  migrated at start. New accounts without an explicit role become users. Users get no cloud
  or sub-agent tools and cannot change the knowledge base.
- **Knowledge base uploads** go through *Monitor › Hochladen*; files dropped into the chat
  belong to the account's own area.
- **Argus addresses the person who is logged in.** The name is derived from the account name
  ("anna.schmidt" becomes "Anna Schmidt"). The global `OWNER_NAME` setting is gone.
- **Model.** llama.cpp with Qwen3.6-35B-A3B, experts partly in RAM, instead of SGLang with a
  9B model. The model file is exchangeable through `.env`.
- **Rejected steps are shown as rejected.** A tool call refused by the lock after company
  data carries a lock symbol and "gesperrt" in the chat instead of a check mark.
- Relative paths in the file tools resolve against the workspace (`Uploads/report.pdf`); the
  note inserted when a file is dropped into the chat no longer names a Windows path.
- **EPUB files are read by Argus' own code.** The package EbookLib (AGPL) is no longer a
  dependency; no Python package of the backend is under GPL or AGPL.
- **Document search** is hybrid: bge-m3 vectors plus BM25, fused in Qdrant, reranked in a
  separate container. Measured on 60 questions: correct document first 67 % → 96 %,
  19.9 s → 3.5 s per search. The backend no longer loads models itself (no PyTorch).

### Security

- **The code sandbox no longer mounts the workspace.** It used to see the files of every
  account read-only. Now the backend sends it only the files of the requesting account, runs
  are executed one at a time, and before and after each run all leftover processes and
  everything in `/tmp` are removed. PowerShell is no longer part of the sandbox image.
- Administrators' file tools (`read_document`, `ingest_document`, `fs_*`) cannot reach
  the areas of other accounts.
- **Code execution through `calculate`.** The expression evaluator in the sandbox passed
  string literals on to sympy, which evaluates them as Python. A crafted expression could run
  code in the sandbox and read the mounted workspace. Inherited from the earlier edition. The evaluator now
  rejects strings, attribute access, non-ASCII characters, backslashes and comments, and
  runs without Python builtins.
- **Attached files lock the conversation from the start.** A message that names a file in
  the uploads folder marks the conversation as holding company data before the agent runs.
  Before, the model could search the web with the file name (a case number, a customer
  name) and read the file afterwards; the lock came one request too late.
- Sub-agents get their tools filtered by the role of the account; an empty tool list no
  longer falls back to all tools.
- An account without a stored role is treated as a user, never as an internal run.
- Changing an account's role ends its sessions.
- The form of address taken from the account name accepts Latin letters only, three words
  at most, so that an account name cannot carry text into the system prompt.
- A task-wide approval applies only to the account that gave it.
- Page fetches read at most `WEBPAGE_MAX_BYTES` (3 MB); a page that announces more is not
  loaded at all. Before, the whole response was held in memory before it was cut.

### Added

- **Audit of logins and account changes, forwarding to a SIEM.** Logins (successful, failed,
  locked), API logins, logouts, setup, password changes and every account change by an
  administrator go into the HMAC-chained audit log, with account, address and reason but
  never a password; an unknown account name is stored only as a hash. `AUDIT_SIEM=syslog`
  (RFC 5424 over UDP or TCP) or `stdout` forwards every audit entry, as JSON or CEF
  (`AUDIT_SIEM_FORMAT`), without blocking a request. The monitor shows the new events by
  name.
- **Default prompts in a company tone.** Argus speaks like a factual colleague, judges
  company documents (contracts, offers, policies, data sheets, test reports) against what is
  customary and names the function that should sign off at the end, instead of a disclaimer.
  A confidentiality section keeps internal names, figures and file names out of web
  searches; a file it cannot find leads to a request to attach it, not to a web search.
  `ARGUS_ANREDE=Sie` switches from "du" to "Sie".
- **One file area per account.** Every account, not only administrators, can attach files
  in the chat (drag and drop or paper clip), let Argus read and analyse them in the sandbox
  and download the results (download pills above the answer, dialog *Meine Dateien*).
  Files live under `users/<id>/Dateien` and `users/<id>/Ergebnisse`; all access goes through
  `rag_backend/user_files.py`. Quota `USER_FILES_MAX_MB` (500), retention
  `USER_FILES_RETENTION_HOURS` (168).
- **Guided setup.** `install.cmd` (Windows, plain batch) and `install.sh` (Linux) start
  `setup/install.py`: it checks or, after asking, installs Python and Docker Desktop,
  checks GPU and RAM, asks for folders and the operating mode, creates secrets and `.env`,
  downloads the models with resume and SHA-256 verification (`setup/models.json`, pinned
  revisions), builds and starts the stack and waits until it answers.
- **Network isolation.** Model, embedding, reranker, databases, speech and tracing run in a
  network without egress; `setup/check_networks.py` checks the rules in CI.
- **Lock after company data.** Once a conversation has read documents or files, every outbound
  path in it is blocked (`EGRESS_AFTER_SENSITIVE`).
- **Redacted traces** (`TRACE_CONTENT`), injection check over the full text of fetched pages,
  canary tokens.
- **Setup token for the first account.** `init_stack.py` generates `DASHBOARD_SETUP_TOKEN`; the
  setup page asks for it.
- **Documents found are listed above the answer.** After a document search the chat shows
  which documents the passages came from, with their relevance. The list comes from the
  search result, not from the model's text, and is stored with the conversation.
- **Password change dialog** that requires the current password.
- **`DASHBOARD_TRACES_URL`**: address of the tracing UI for the monitor tab, for installations
  where the browser is not on the same machine.
- Tests for the backend (CI), measurements for document search and the whole agent (`eval/`).
- README: what Argus is for, how it compares to a cloud assistant, measured values, how web
  search is secured, which models can replace the measured one; screenshots of this version.

### Removed

- **Machine control.** The former core feature, operating the Windows machine through SSH
  and PowerShell, is gone: the tools, the SSH client, the setup script for the `argus`
  account and every setting for it. No container holds credentials for a host.
- The mode `EGRESS_AFTER_SENSITIVE=confirm`; it now blocks.
- Telegram (approvals now only through the card in the chat), Open WebUI, the OpenAI proxy
  endpoint, the Intune wallpaper, the outdated configuration reference in `docs/`.

### Fixed

- Timestamps from the backend were read as local time in the interface; times were off by the
  UTC offset and chats after midnight appeared under "yesterday".
- After creating the first account and logging out, the page showed the setup form again
  instead of the login.
- The monitor tab embedded tracing from a fixed `127.0.0.1` address.
- A wrong password reported "not logged in" instead of the backend's message.
- With the cloud module enabled, the router started a background mission for any account;
  now only for administrators.
- Deleting an account left its long-term memory behind. A reflection still running while the
  account was deleted could also write it again; it now checks that the account exists.
- Research questions were never planned by the cloud model, because the check read the
  question's category before the router had set it. Planning now runs for administrators
  with the cloud module; for users the request never leaves the machine.
- The router and the solver still offered PowerShell, which the sandbox no longer has.
- The API description (`/docs`) was readable without login; it is off unless `API_DOCS=true`.
