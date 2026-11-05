# Configuration reference

*English · [Deutsch](configuration.de.md) · [Back to the README](../README.md)*

Where the switches live and how to apply them is explained in the
[README](../README.md#configuration). This page covers the individual areas.

## Keys and secrets — `API_Tokens/`

The folder is excluded via `.gitignore` and restricted to your own user on the host.
Anything you enter there in plain text is replaced by `Setup1.py` on its next run with an
encrypted value (`ENC:…`, Fernet with `fernet_key.txt`). The containers get the files
mounted as `/run/secrets` and decrypt in-process.

| File | Created by | Required? | Purpose |
|---|---|---|---|
| `HF_TOKEN.txt` | you | yes | model downloads from HuggingFace |
| `Gemini.txt` | you | no | cloud missions and `cloud_ask`; empty = cloud stage off |
| `telegram.txt` | you | no | bot token and chat ID, two `KEY=VALUE` lines |
| `LANGSMITH_API_KEY.txt` | you | no | tracing to the LangSmith cloud instead of local Phoenix |
| `ssh_user.txt`, `ssh_key`, `ssh_key.pub`, `ssh_password.txt` | `setup_ssh.ps1` | — | the action engine's access to the host; the password is emptied once key auth is in place |
| `fernet_key.txt` | `Setup1.py` | — | the key behind everything encrypted: secrets, chat histories |
| `jwt_secret.txt`, `webui_secret_key.txt`, `service_token.txt` | `Setup1.py` | — | logins, Open WebUI coupling |
| `postgres_password.txt`, `sglang_api_key.txt`, `searxng_secret_key.txt`, `audit_hmac_key.txt` | `Setup1.py` | — | internal services, audit log signature |

`fernet_key.txt` is the master key. Lose it and histories and encrypted tokens become
unreadable; that is why `Setup1.py` stops hard on an invalid key instead of quietly
generating a new one.

## Setting up Telegram

1. Open `@BotFather` in Telegram, `/newbot`, pick a name. The token looks like
   `8123456789:AAF…`.
2. Message your new bot — anything, so that a chat exists.
3. Get your chat ID: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser;
   the JSON contains `"chat":{"id":123456789,…}`.
4. Fill in `API_Tokens/telegram.txt`:

   ```
   TELEGRAM_BOT_TOKEN=8123456789:AAF…
   TELEGRAM_CHAT_ID=123456789
   ```

5. `python Setup3.py` (validates and encrypts the values), then
   `docker compose up -d rag-backend`.

The bot serves **exactly this one chat ID**; everyone else is ignored. It understands
text, voice messages (transcribed; reply optionally as speech — `/voice` toggles) and
photos. The same channel carries the confirmation cards for write actions and the results
of cloud missions.

Without Telegram, approvals work in the Argus chat through a card in the conversation.
Only the Open WebUI path cannot show a card — there, Telegram remains mandatory for write
actions.

## Cloud stage (Gemini)

Create a key in [Google AI Studio](https://aistudio.google.com/), put it in
`API_Tokens/Gemini.txt`, run `python Setup4.py`, then `docker compose up -d rag-backend`.

What this enables: `cloud_ask` (a single question to the large model, for instance after
the local agent has failed a host task several times) and **missions** — planner,
workers, reviewer, with web search through your own SearXNG. Model chains, rate limits
(preset for the free tier: 8 requests per minute, 200 per day), number of rounds and
result size live in the `SETUP4` block of `Setup4.py`.

Before every outbound call the pseudonymisation runs: e-mail, IBAN, phone numbers and
dates of birth by pattern, free-form names by the local model acting as a named-entity
recogniser, replaced by placeholders such as `[PERSON_1]`. If that step fails, **nothing is
sent** (`CLOUD_REDACTION_FAIL_CLOSED`). To switch the stage off entirely: leave
`Gemini.txt` empty or set `CLOUD_AGENTS_ENABLED` to `false`.

## Switching off machine control

If you only want chat, documents and research: at the top of `Setup3.py`, set
`ACTION_ENGINE_ENABLED = "false"`, run `python Setup3.py`, then
`docker compose up -d rag-backend`. The host tools are then never registered, the backend
opens no SSH connection, and `setup_ssh.ps1` need not have run — the `argus` user is not
needed.

## Enabling vision

The model is multimodal, but the image encoder costs VRAM that otherwise belongs to the
context. That is why the path is off by default. Enable it in `Setup2.py`, `LLM_CONFIG`:

```python
"sglang_enable_multimodal": "true",
"max_images_per_request":   2,       # about 1600 tokens per image
```

Then `python Setup2.py` and `docker compose up -d sglang rag-backend`. After the first
start with vision, check the SGLang log for what is left of the KV pool:

```bash
docker compose logs sglang | grep max_total_num_tokens
```

If the value is well below the requested `sglang_kv_pool_tokens`, adjust the context
accordingly (next section). Images then come in by dragging them into the chat or as a
photo via Telegram; text inside images is treated as data, not as instructions.

## Increasing the context

Four values in `LLM_CONFIG` (`Setup2.py`) determine the window:

| Value | Meaning |
|---|---|
| `sglang_max_sequence_len` | upper bound SGLang is asked for (`--context-length`) |
| `sglang_kv_pool_tokens` | requested KV pool (`--max-total-tokens`) — **a request, not a promise** |
| `effective_context_tokens` | cap the agent sets itself; what binds is always the smaller of this and the actually measured pool |
| `sglang_mem_fraction_static` | the OOM knob: share of VRAM SGLang may take (0.90) |

At start-up SGLang takes the smaller of the request and what fits into VRAM, and writes
the result to its log as `max_total_num_tokens`. The agent queries that real value at
runtime and plans with it — which is why you can set the three token values generously
without breaking anything. With more VRAM (24 GB and up) it pays to raise all three to
40,000 or more and leave `mem_fraction` at 0.90. If SGLang dies with out-of-memory, lower
`mem_fraction` or shrink the request.

## Swapping the model

The swap is one entry in `LLM_CONFIG`, but three things decide whether it works:

```python
"model_name":            "QuantTrio/Qwen3.5-9B-AWQ",
"sglang_model_revision": "938f8e3ef86c9d1e9bec3705e149694c172592f1",
"sglang_quantization":   "",            # empty: detected from the model config
"sglang_dtype":          "float16",
"tool_call_parser":      "qwen3_coder",
"reasoning_parser":      "qwen3",
```

**Move the revision along.** SGLang loads with `--trust-remote-code`, so it executes Python
from the HuggingFace repository. Pinning to a commit is supply-chain protection and must
be set anew when the model changes:

```bash
curl -s https://huggingface.co/api/models/<repo> | python -c "import json,sys;print(json.load(sys.stdin)['sha'])"
```

**Pick the tool-call parser to match the model.** This is the part that breaks most often
on a swap. The agent only receives tool calls if SGLang understands the model's format:
Qwen3.5 emits its calls as XML in the `qwen3_coder` style, older Qwen and most models with
a Hermes template as JSON (`hermes` or `qwen`). With the wrong parser SGLang reports
`Failed to parse JSON part`, and the agent answers empty or never calls a tool. Do not
rely on `auto` — for Qwen3.5 it guesses wrong. The same goes for `reasoning_parser`: it
must match the model's thinking tags, otherwise the reasoning ends up in the answer.

**dtype and quantisation.** Qwen3.5 (GDN architecture) needs `float16` for the model and
its conv cache, otherwise the first forward pass crashes in the Triton kernel; another
model will not need that. AWQ is detected from the model config; GPTQ or FP8 go into
`sglang_quantization`. The model plus KV pool must fit into VRAM — the 13 GB of the 9B AWQ
are the ceiling on 16 GB.

Afterwards `python Setup2.py`, `docker compose up -d sglang rag-backend`, check the SGLang
log for `max_total_num_tokens` and run the test suite (see [Operations](../README.md#operations)). The
backend contains a small normaliser for doubly JSON-encoded tool arguments, as Qwen
sometimes produces them; other models have other quirks that only show up in use. Vision
and language (`SOUL.md` sets German and the informal "du") are properties of the model,
not of the system.

## Voice

Without a reference voice Argus speaks with one of the built-in voices (`QWEN_VOICE` in
`Setup1.py`: `vivian`, `serena`, `ryan`, `aiden`, `eric`, `dylan`, `uncle_fu`, `ono_anna`,
`sohee`). For your own voice a single recording is enough:

- `voice_ref/stimme.wav` — 10 to 20 seconds, one speaker, calm and without reverb; any WAV works, the sample rate is converted to 24 kHz on load
- `voice_ref/stimme.txt` — the spoken text, word for word

Then set `"QWEN_CLONE_REF_WAV": "/app/voice_ref/stimme.wav"` in `Setup1.py`, run
`python Setup1.py`, then `docker compose up -d tts-service`. The voice is loaded once at
start and used for every reply; if the file is missing, the service falls back to the
preset voice instead of dying. Keeping `QWEN_SEED` fixed stops the voice drifting from
sentence to sentence.

The repository ships **no** reference voice. Use your own, or one you hold the rights to —
cloning another person's voice without their consent is not acceptable, and film audio is
copyrighted on top.

## Further switches

In `Setup1.py`: rate limits per endpoint, self-registration (`DASHBOARD_ALLOW_REGISTER`,
new accounts are always `chat`), login lifetimes (`JWT_EXPIRE_DAYS`,
`DASHBOARD_SESSION_HOURS`), retention of uploaded files (`UPLOAD_RETENTION_HOURS`),
tracing locally in Phoenix or to the LangSmith cloud, the host folder for eval runs and
audit files (`ARGUS_EVALS_DIR`, default `C:\Argus_Workspace\Evals`). In `Setup2.py`: chunking, number of
sources read, sub-agent time limits, size of the memory excerpt. Also in `Setup1.py`: the
speech service's audio cache (`TTS_CACHE_MAX_ENTRIES`, `TTS_CACHE_MAX_MB`), which makes a
second read-aloud of the same text free. In `Setup3.py`: approval
wait time (`CONFIRMATION_TIMEOUT_SECONDS`), lifetime of a task approval
(`TASK_APPROVAL_TTL_SECONDS`), output caps of the host tools. Every value carries a
comment in the script saying what it does.
