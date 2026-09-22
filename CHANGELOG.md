# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/lang/en/).

## [1.0.0] — 2026-09-22

First public release. Argus has been running on its own machine for months; this is
the point where the setup scripts were made portable enough to hand to someone else.

### Added

- **`route_distance` tool.** Driving distance and travel time between two addresses via
  OpenStreetMap (Nominatim for geocoding, OSRM for the route). Returns one way, round trip
  and, with `days_per_week`, the weekly, monthly (52/12) and yearly mileage already worked
  out. The resolved addresses come back with the answer so an ambiguous place name is
  visible instead of silently wrong. Endpoints are configurable in `Setup2.py` for anyone
  running their own Nominatim/OSRM; the public services' fair-use rules (one request per
  second, a real user agent) are honoured, and geocoding results are cached.
- **Audio cache in the speech service.** Same text, same voice, same format produce the
  same bytes, so reading a paragraph aloud a second time no longer costs a full synthesis
  (measured: 8.6 s → 0.00 s). LRU by count and size (`TTS_CACHE_MAX_ENTRIES`,
  `TTS_CACHE_MAX_MB`), keyed by a hash rather than the text itself. The dashboard also
  keeps the last eight paragraphs as blobs.
- **Markdown tables in the chat**, including column alignment taken from the separator
  row. Escaping still runs before any markup, so cell content is never treated as HTML.
- **`SUBAGENT_MAX_STEPS`** makes the sub-agent step budget configurable.
- **Owner name is configurable** through `OWNER_NAME` in the `.env` or `owner_name` in
  `Setup2.py`; the identity seed files carry an `{{OWNER}}` placeholder.
- **`ARGUS_EVALS_DIR`** replaces a hard-coded path for eval runs and audit files.
- `SECURITY.md` with a private reporting route, and a German README alongside the English
  one.

### Changed

- **A sub-agent that runs out of steps no longer throws its work away.** It used to return
  nothing but `Error: sub-agent exceeded the step limit (15)`; in one observed trace that
  discarded 4:50 and 2:53 minutes of reading, after which the main agent started the same
  search over. Now a final call without bound tools makes the worker write its report from
  what it already gathered.
- **Machine control can be switched off cleanly.** With `ACTION_ENGINE_ENABLED = "false"`
  in `Setup3.py` the host tools are never registered, no SSH connection is opened, and
  `setup_ssh.ps1` need not have run at all.
- Error strings from `calculate`, `execute_code` and `get_weather` now use the `ERROR:`
  prefix, so a failed call is counted as a failed attempt instead of resetting the counter.
- A tool result that failed but still carries partial output keeps that output instead of
  discarding it.

### Fixed

- **PowerShell classification gate: two ways past the confirmation prompt.** The braced
  variable form `${c} = "Remove-Item"` fell out of the "an assignment is never a read"
  rule, and combined with a harmless-looking command head it was classified READ — that is,
  executed immediately, as administrator, without asking. Invoking a command through a
  variable (`& $c`, `. ${c}`) now counts as destructive, and both variable spellings are
  handled. Regression tests cover it.
- **Truncated tool-call markup.** The XML fallback required a closing `</tool_call>`; when
  the answer budget ran out mid-call that tag is missing, the call went unnoticed, and the
  worker returned its bare intention as a research result. Truncated blocks are now
  accepted when they contain at least one complete parameter, and tool markup is stripped
  from rescued text.
- The Telegram bot token is redacted from log output by a filter on the root handlers,
  including tracebacks — raising the log level alone only helps as long as nothing writes
  it at WARNING or above.

### Security

- Container images are pinned by digest, the language model to a revision.
- Everything that leaves the machine through the optional cloud stage is pseudonymised
  first, fail-closed: if detection fails, nothing is sent.

[1.0.0]: https://github.com/Donofluziver-dev/Argus/releases/tag/v1.0.0
