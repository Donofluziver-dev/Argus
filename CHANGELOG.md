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
  note inserted when a file is dropped into the chat no longer names a Windows path. The
  file tools also answer with such paths, error messages and the tool guide name none, and the
  monitor shows the evals folder as it is on the host (`ARGUS_EVALS_DIR`) instead of a fixed
  `C:\Argus_Workspace`. Windows paths from older conversations are still accepted.
- **EPUB files are read by Argus' own code.** The package EbookLib (AGPL) is no longer a
  dependency; no Python package of the backend is under GPL or AGPL.
- **Document search** is hybrid: bge-m3 vectors plus BM25, fused in Qdrant, reranked in a
  separate container. Measured on 60 questions: correct document first 67 % → 96 %,
  19.9 s → 3.5 s per search. The backend no longer loads models itself (no PyTorch).
- **The rate limit counts per account.** Behind a proxy all users shared one limit (for the
  chat 60 requests per minute for the whole company), because it counted per address. Now a
  valid session or API token is the key; login, setup and registration still count per
  address.

### Security

- **`CLOUD_AGENTS_ENABLED` now closes every path to a cloud model.** Two helper paths
  (research plan and plan review, debugging help in the coding loop) only checked whether a
  key was stored and sent redacted text without a confirmation card. They now require
  `CLOUD_AGENTS_ENABLED=true`, `CLOUD_MISSION_AUTO_APPROVE=true` and an administrator's turn;
  otherwise Argus works locally. The switch defaults to off in the code as well; before, a
  missing `.env` entry meant on.
- **A mission recognised by the router waits for the confirmation card.** When the router
  classified an administrator's message as a background mission, it started the mission
  directly, without the card that the `start_mission` tool shows. It now goes through that
  tool: the same card with the redacted preview, the same audit entry, and nothing is sent
  before the card is approved. Only `CLOUD_MISSION_AUTO_APPROVE=true` starts directly, and
  never in a conversation with company data. With the cloud module off or no provider
  configured there is no card, just the reason.
- **The per-account memory could carry company data past the lock.** The file tools could
  write `users/<id>/MEMORY.md`, which is loaded into the system prompt of every new
  conversation, including other accounts' memory. The whole `users/` folder is now closed to
  the file tools, and the reflection skips conversations that hold company data.
- **Sandbox: code no longer runs as the same user as the sandbox service.** Code runs as
  `runner`, calculations as `calc`, both without capabilities; the service keeps `SETUID`,
  `SETGID` and `KILL` only. Between runs the sandbox ends every process of the run user and
  verifies that none is left, instead of one pass over the process list; if that cannot be
  confirmed, or the service was started without these rights, it refuses to execute.
  `calculate` runs in its own process with a hard time limit; before, the calculation went on
  in a thread after the timeout.
- The backend rejects requests coming from the sandbox container, which shares a Docker
  network with it. The check looks at the real TCP peer, before any forwarding header.
- **Client address and scheme only from trusted proxies.** `X-Forwarded-For` and
  `X-Forwarded-Proto` count only from addresses in `ARGUS_TRUSTED_PROXIES` (default: none,
  `*` is refused); uvicorn itself runs with `--no-proxy-headers`. Rate limit and audit log
  see the real client behind the proxy and cannot be fed a made-up address.
- **Injection check:** on by default in the code (`WEB_INJECTION_GUARD`), and content that
  cannot be checked is not used. Before, a failed check let the content through.
- A session cookie is no longer accepted as an API bearer token.
- **LangSmith is gone.** The key is no longer mounted or loaded and tracing to LangSmith is
  forced off; traces go to Phoenix inside the stack only.
- The cloud provider no longer writes the first characters of an API key to the log.
- **The code sandbox no longer mounts the workspace.** It used to see the files of every
  account read-only. Now the backend sends it only the files of the requesting account, runs
  are executed one at a time, and before and after each run all leftover processes and
  everything in `/tmp` are removed. PowerShell is no longer part of the sandbox image.
- **A fresh sandbox container for every code run.** A sandbox container accepts exactly one
  `/execute` and exits afterwards (once the answer is sent, at the earliest 10 s after its
  start, so Docker does not back off), Docker restarts it with an empty `/tmp` and new
  namespaces. Two replicas (`ARGUS_SANDBOX_REPLICAS`) keep runs from waiting; the backend tries
  them in turn on fresh connections and never repeats a request that was already sent. No
  container gets the Docker socket. The block against requests from the sandbox also covers
  the addresses of freshly started replicas.
- **No SysV IPC in the sandbox.** Shared memory segments, message queues and semaphores
  survived the cleanup between runs, and all runs are the same user, so a later run of
  another account could read them. The sandbox container now gets its own IPC namespace
  with all SysV limits at zero; `multiprocessing` keeps working on POSIX semaphores.
- Administrators' file tools (`read_document`, `ingest_document`, `fs_*`) cannot reach
  the areas of other accounts.
- **Code execution through `calculate`.** The expression evaluator in the sandbox passed
  string literals on to sympy, which evaluates them as Python. A crafted expression could run
  code in the sandbox and read the mounted workspace. Inherited from the earlier edition. The evaluator now
  rejects strings, attribute access, non-ASCII characters, backslashes and comments, and
  runs without Python builtins.
- **The lock after company data also sees re-encoded data.** With
  `EGRESS_AFTER_SENSITIVE=allow`, outbound requests were compared with the fingerprints of
  the company data only as plain, URL- or HTML-encoded text, so a hijacked model could send
  the data as Base64. Outbound arguments are now also decoded from Base64 (standard and
  URL-safe), Base32, hex, gzip, zlib and deflate (at most 1 MB; anything beyond blocks),
  ROT13, reversed text, full-width characters and characters separated one by one, nested up
  to three levels. Long random-looking sequences (encrypted or packed) block in a marked
  conversation; ordinary URLs, UUIDs and hashes do not. A request above 200 kB or a failed
  check blocks as well. Paraphrase, translation or splitting over many requests is still not
  recognised; the default `block` is unaffected.
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

- **Linux server in one command (`setup/bootstrap.sh`).** `curl -fsSL …/setup/bootstrap.sh |
  bash` checks the server first and says what is missing (CPU architecture, GPU and driver 570
  or newer, Docker with Compose, NVIDIA Container Toolkit with a test container, RAM, disk space
  for images and models, proxy, reachability of GitHub, Hugging Face, Docker Hub, ghcr.io, PyPI
  and Debian), then fetches the code to `~/argus/app` (git or archive) and starts the guided
  setup, with questions from the terminal even under `curl | bash`. Checked by ShellCheck in CI.
- **Choice of language model at setup.** The installer asks for Qwen3.6-35B-A3B (default) or
  Gemma 4 26B-A4B (`--llm qwen|gemma`), downloads only the chosen one and writes its model file,
  MTP head, image encoder and alias to `.env`. Gemma reads images with its own encoder; on a
  card with room to spare it also gets `LLM_UBATCH=2048` for its full image token budget. `doctor`,
  the offline bundle and `--models-only` follow the model in `.env`; a model that is not in
  `setup/models.json` is left alone.
- **GPU split from the GPU memory.** The installer computes `LLM_N_CPU_MOE` and
  `LLM_MMPROJ_OFFLOAD` from the GPU memory and the model's needs in `setup/models.json`: on 16 GB
  the measured 25 layers in RAM, on a large card (about 31 GB and up for Qwen) everything on the
  GPU. Own values stay as long as the model stays.

- **Web access is a compose profile (`web`: SearXNG, `local-proxy`).** Without it Argus offers
  the model no tool that reaches the internet (web search, page fetch, research, weather,
  route), the prompts do not describe them, `/v1/dashboard/web_search` answers 503, and the
  router does not ask for web research; only the cloud module remains, if it is on. The
  detection of optional services moved into `rag_backend/features.py`. Installer `--web`,
  `--no-web`. **Update note:** `ARGUS_ENV_VERSION=2` marks the migrated `.env`;
  `init_stack.py` (or the installer) adds `web` to the profiles of an older one, because
  SearXNG always ran before. Until then web access is off and `doctor` warns.
- **Installation without internet** (`install bundle`, `install --bundle <dir>`). The bundle
  holds every image of every profile, the source of the last commit, the Python wheels of the
  installer and optionally the models, with a SHA-256 over the images. Third-party images are
  pulled by digest and renamed for the bundle, because `docker save`/`load` does not keep
  digests in every image store. On the target the installer verifies, loads and starts without
  building or downloading anything.
- **Images from a registry.** Workflow *Images* (manual or tag `v*`) pushes the own images to
  ghcr.io; `install --image-prefix ... --image-version ...` uses them. Built services now have
  fixed names (`argus/<service>:local`, independent of the project name) and
  `pull_policy: never`: before, a missing image name could be pulled from Docker Hub instead of
  being built.
- **Speech and tracing are compose profiles** (`voice`: `stt-service`, `tts-service`;
  `tracing`: `phoenix`), set in `COMPOSE_PROFILES`, default `voice,tracing`. A server can do
  without the 4.4 GB speech build or without Phoenix (Elastic License). The backend detects
  over DNS which services run: without speech the interface hides microphone and read-aloud
  and `/stt`, `/tts` answer 503 "not set up"; without Phoenix no trace exporter is set up, and
  one that appears later is picked up. The installer asks for both (`--voice`, `--no-voice`,
  `--tracing`, `--no-tracing`), `doctor` reports the profiles. **Update note:** an older
  `.env` without the key gets `voice,tracing` the next time the installer or
  `setup/init_stack.py` runs; until then speech and tracing are off and `doctor` warns.
- **TLS proxy for a server** (compose profile `proxy`, off by default): Caddy in front of
  `rag-backend`, with its own CA (`internal`), the company's certificate (`files`) or Let's
  Encrypt (`acme`). It runs as `ARGUS_UID` without capabilities and with a read-only file
  system, only in the new network `edge` shared with `rag-backend`, redirects HTTP to HTTPS,
  sets HSTS and does not buffer the chat stream. The installer asks for it in server mode
  (`--proxy`, `--no-proxy`, `--tls-mode`); `doctor` checks name, origins, data folder and
  whether `ARGUS_UID` can read the certificate.
- **Linux setup without surprises:** the installer tells a missing `docker` group apart
  from a stopped Docker service, warns when it runs under `sudo` and then takes the user
  behind `sudo` as `ARGUS_UID` instead of a fixed 1000. With SELinux enforcing it offers the
  `:z` label for every bind mount (`ARGUS_MOUNT_LABEL=,z`) and refuses it for system folders
  and whole home directories; `doctor` checks both.
- **`install.cmd doctor` / `./install.sh doctor`** checks an installation without changing
  anything: Docker and GPU, memory and disk space, `.env` against `.env.example`, secrets and
  folder permissions, models (`--verify-models` adds SHA-256), network isolation, the state of
  the stack and risky switches. It never prints secret values.
- **`install.cmd reset-password <account>`** resets a password through the backend container
  (`python -m rag_backend.admin_cli`), also for the last administrator. The password is read
  from standard input, all sessions of the account end and the reset is audited.
- **OpenDocument text (`.odt`)** is read and indexed.
- **Images and scanned PDFs.** The model service loads the model's image encoder
  (`LLM_MMPROJ_FILE`, `mmproj-Qwen3.6-35B-A3B-F16.gguf`, 0.9 GB, downloaded and checked by
  SHA-256 by the installer). A PDF page with fewer than 20 characters of text and an image is
  treated as a scan: the local model transcribes it and the text takes the page's place,
  marked as read from the image. Image files (PNG, JPG, WebP) are read the same way, in
  document search, `read_document` and the monitor upload. The text counts as company data.
  At most `SCAN_MAX_PAGES` (30) pages and `SCAN_MAX_SECONDS` (900) per file; an unreadable page
  does not stop the file. The backend asks the model service whether it accepts images
  (`/props`), so the chat's image button appears by itself; `LLM_ENABLE_MULTIMODAL` still
  forces it, the old name `SGLANG_ENABLE_MULTIMODAL` is no longer read. The encoder sits in
  RAM by default (`LLM_MMPROJ_OFFLOAD=false`): on a 16 GB card VRAM stays at about 14.4 GB and
  an image takes about 11 s. Without the file the model service starts without image input
  and logs a warning. **Update note:** `init_stack.py` adds `LLM_MMPROJ_FILE` to an existing
  `.env` that uses the default model, and `LLM_MMPROJ_OFFLOAD`; the file itself comes with the
  installer's model step.
- **Scan measurement (`eval/scans`).** Eleven fictional test pages generated in code (fax,
  phone photo, dense table, handwriting, form with ticks, technical drawing, two columns,
  stamp over an amount, an instruction to AI systems inside the image, a photo without text)
  and stress series on one test report table (resolution, blur, JPEG, faint print, rotation,
  print size, harder handwriting). Checks count values and words of every table row
  separately. Result with Qwen3.6: 111 of 111 checks; wrong digits, unmarked, only below
  150 dpi or with 4 pt print. `--abschriften` re-scores saved transcriptions without the model.
- **Audit of logins and account changes, forwarding to a SIEM.** Logins (successful, failed,
  locked), API logins, logouts, setup, password changes and every account change by an
  administrator go into the HMAC-chained audit log, with account, address and reason but
  never a password; an unknown account name is stored only as a hash. `AUDIT_SIEM=syslog`
  (RFC 5424 over UDP or TCP) or `stdout` forwards every audit entry, as JSON or CEF
  (`AUDIT_SIEM_FORMAT`), without blocking a request. The monitor shows the new events by
  name.
- **Entries the SIEM does not receive are re-sent.** When sending fails or the queue is full,
  the entry goes to a spool file in the `rag_data` volume (`AUDIT_SIEM_SPOOL_DIR`, files
  `0600`, at most `AUDIT_SIEM_SPOOL_MAX_MB`, 50 MB) and is re-sent, oldest first and before
  new entries, once the SIEM answers again; after a send error Argus pauses with a growing
  interval (up to 30 s). Delivery is at least once; the SIEM can drop duplicates by the
  entry's HMAC signature (`hmac`), which differs for every entry. The backend and the MCP file server each write their own file; a
  process takes over the files of a crashed one by an atomic rename, so no file is sent
  twice at the same time. Only beyond the limit are entries counted as lost. Over UDP, losses
  on the way go unnoticed.
- **Default prompts in a company tone.** Argus speaks like a factual colleague, judges
  company documents (contracts, offers, policies, data sheets, test reports) against what is
  customary and names the function that should sign off at the end, instead of a disclaimer.
  A confidentiality section keeps internal names, figures and file names out of web
  searches; a file it cannot find leads to a request to attach it, not to a web search.
  `ARGUS_ANREDE=Sie` switches from "du" to "Sie".
- **MTP draft file for other models.** `LLM_MTP_MODEL_FILE` names a separate MTP file, as
  Gemma 4 ships it; Qwen3.6 keeps its MTP head inside the model file.
- **Sampling per model.** `LLM_TEMPERATURE`, `LLM_TOP_P` and `LLM_TOP_K` replace the values
  tuned for Qwen, so another model can run with the settings from its model card.
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

- Indexing an upload in the monitor reported every file as "0 Chunks": the interface read a
  field the server did not send. The server now returns the number of chunks, and a failed
  file shows its reason.
- **The stack could not start on Linux.** The secrets were created with mode `0600`; the
  model services run as root without capabilities and the backend and speech services as uid
  1000, so none of them could read them unless the installing user happened to be uid 1000.
  `API_Tokens/` now gets `0700` and the files `0644`. The backend runs as the installing
  user (`ARGUS_UID`, `ARGUS_GID` in `.env`), the installer creates the workspace, uploads,
  evals and model folders, and the backend image runs under any uid.
- SearXNG mounted the `searxng_config` folder writable; it now gets only `settings.yml`,
  read-only.
- `install.cmd` is checked out with CRLF line endings; `cmd.exe` is unreliable with labels in
  LF files.
- **File names keep umlauts and other letters.** "Prüfbericht.pdf" became "Pr_fbericht.pdf",
  in the chat and in every passage of the document search, and files with umlauts placed into
  `Uploads` by hand could not be indexed. Names are normalised to NFC; Windows device names
  are rejected. Backend and sandbox use the same rule.
- Uploads of administrators were deleted after 24 hours even when they had not been indexed
  yet; only indexed files are cleaned up now.
- `.doc` and `.rtf` could be uploaded but not read; they are no longer accepted, and
  `read_document` tells an unsupported type, an image and a scan apart.
- Voice recordings between 25 and 50 MB ended in "STT service unreachable"; the limit is now
  25 MB on both sides and the message says the recording is too large.
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
- **Long-term memory was never updated in normal chat.** Only following a mission recorded
  activity, so the reflection that runs after `REFLECTION_IDLE_SECONDS` of idle time never
  started. The chat now records the time of each turn; the reflection runs once per idle
  phase, with the local model, and still skips conversations that hold company data.
- Research questions were never planned by the cloud model, because the check read the
  question's category before the router had set it. Planning now runs for administrators
  with the cloud module; for users the request never leaves the machine.
- The router and the solver still offered PowerShell, which the sandbox no longer has.
- The API description (`/docs`) was readable without login; it is off unless `API_DOCS=true`.
