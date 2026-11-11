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
  migrated at start. New accounts without an explicit role become users. Users get no file
  tools and no code sandbox until each account has a workspace of its own.
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

### Added

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
- Deleting an account left its long-term memory behind.
- The API description (`/docs`) was readable without login; it is off unless `API_DOCS=true`.
