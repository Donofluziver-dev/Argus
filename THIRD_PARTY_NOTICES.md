# Third-party notices

Argus itself is licensed under the Apache License 2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)). It builds on software, fonts and models from others. This file lists them
by how they relate to Argus, because that decides what their licenses ask for.

Licenses are given as reported by the projects (package metadata, repository license file,
model card) in November 2026. Versions change; check the current state before you redistribute.
This is the maintainer's understanding, not legal advice.

## 1. Included in this repository

| Component | Where | License | Note |
|---|---|---|---|
| Barlow, Chakra Petch, JetBrains Mono (fonts) | `rag_backend/static/fonts/` | SIL Open Font License 1.1 | License texts are in the same folder. |
| `tts-server.patched.cpp` | `tts_service/` | MIT | Modified copy from [qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp); the copyright notice is in the file header. |

The browser interface uses no third-party JavaScript or CSS libraries.

## 2. Installed into the images at build time

These are downloaded when the images are built. They are not part of this repository.

### Backend (`rag_backend/pyproject.toml`, direct dependencies)

| Package | License |
|---|---|
| fastapi, sqlalchemy, alembic, argon2-cffi, PyJWT, slowapi, PyYAML, tiktoken, pdfplumber, python-docx, python-pptx, openpyxl, beautifulsoup4, mcp | MIT |
| langchain, langchain-core, langchain-openai, langchain-text-splitters, langgraph, langsmith, langchain-mcp-adapters | MIT |
| qdrant-client, openai, google-genai, python-multipart, tzdata, arize-phoenix-otel, openinference-instrumentation-langchain | Apache-2.0 |
| uvicorn, python-dotenv, httpx | BSD-3-Clause |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| pypdfium2 (bundles PDFium) | BSD-3-Clause, Apache-2.0 |
| psycopg2-binary | LGPL (used unmodified as a library) |
| pytest, pytest-asyncio (tests only) | MIT, Apache-2.0 |

No direct dependency of the backend is under GPL or AGPL. Among the transitive dependencies,
`certifi` is under MPL-2.0. To list every installed package with its license:

```
docker compose run --rm --no-deps --user root --entrypoint sh rag-backend -c "pip install -q pip-licenses && pip-licenses --with-urls"
```

### Other services

| Service | Software | License |
|---|---|---|
| `calc-sandbox` | fastapi (MIT), uvicorn (BSD-3-Clause), sympy (BSD-3-Clause), pandas (BSD-3-Clause), tabulate (MIT), openpyxl (MIT), PowerShell 7 (MIT) | as listed |
| `stt-service` | faster-whisper (MIT), CTranslate2 (MIT), PyAV (BSD-3-Clause), requests (Apache-2.0), cryptography, python-multipart (Apache-2.0) | as listed |
| `tts-service` | qwentts.cpp with ggml (MIT), built from source; OpenBLAS (BSD-3-Clause); fastapi, uvicorn, httpx, cryptography | as listed |
| `stt-service`, `tts-service` | FFmpeg, from the Debian package | LGPL-2.1-or-later and GPL-2.0-or-later parts; the Debian build is a GPL build |
| all Python images | Python 3.13 (PSF-2.0) on Debian; Debian packages under their own licenses | as listed |

## 3. Services started as separate containers

Docker pulls these images from their publishers. Argus does not modify or redistribute them; it
talks to them over the network.

| Service | Image | License |
|---|---|---|
| `llm`, `embed`, `rerank` | `ghcr.io/ggml-org/llama.cpp` | MIT. The CUDA image also contains NVIDIA CUDA runtime libraries under NVIDIA's license terms. |
| `qdrant` | `qdrant/qdrant` | Apache-2.0 |
| `postgres` | `postgres` | PostgreSQL License |
| `local-proxy`, `intern-proxy` | `nginx` | BSD-2-Clause |
| `searxng` | `searxng/searxng` | **AGPL-3.0** |
| `phoenix` | `arizephoenix/phoenix` | **Elastic License 2.0** (source-available, not open source) |

## 4. Models

The models are not part of this repository. The guided setup downloads them from Hugging Face,
each pinned to a revision and checked by SHA-256 (`setup/models.json`).

| Model | Source used by the setup | Used for | License per model card |
|---|---|---|---|
| Qwen3.6-35B-A3B (Alibaba), GGUF | unsloth/Qwen3.6-35B-A3B-MTP-GGUF | chat (default) | Apache-2.0 |
| BAAI/bge-m3, GGUF | gpustack/bge-m3-GGUF | embeddings | MIT |
| BAAI/bge-reranker-v2-m3, GGUF | gpustack/bge-reranker-v2-m3-GGUF | reranking | Apache-2.0 |
| Whisper large-v3-turbo (OpenAI), CTranslate2 conversion | dropbox-dash/faster-whisper-large-v3-turbo (formerly mobiuslabsgmbh) | speech recognition | MIT |
| Qwen3-TTS, GGUF | Serveurperso/Qwen3-TTS-GGUF (loaded when the image is built) | speech synthesis | Apache-2.0 |
| another language model chosen by the operator | — | chat | depends on the model; check its model card before company use |

## 5. What the copyleft and source-available licenses mean here

- **SearXNG (AGPL-3.0)** runs unmodified in its own container, and Argus only sends it HTTP
  requests. Argus' own code is therefore not a derivative of SearXNG. The AGPL applies to
  SearXNG itself: whoever modifies it and lets others use it over a network must offer the
  source of the modified version. The file `searxng_config/settings.yml` is configuration.
- **Arize Phoenix (Elastic License 2.0)** may be used, copied and modified, but may not be
  provided to third parties as a hosted or managed service that gives them access to a
  substantial part of its features. Using it inside one's own organisation is covered.
  Offering Argus to customers as a hosted service together with the Phoenix interface is
  not; in that case leave tracing out or replace it.
- **FFmpeg (GPL build)** is called as a separate program inside the speech images.
  Distributing those images means distributing FFmpeg under the GPL, which includes offering
  its source.
- **psycopg2 (LGPL)** is used unmodified as a library, which the LGPL permits without
  affecting the license of Argus.
- **Docker Desktop** is not part of Argus, but its subscription terms apply to larger
  companies. Docker Engine on a Linux server is not affected.
