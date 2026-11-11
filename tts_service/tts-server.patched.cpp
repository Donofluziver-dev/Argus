// tts-server.cpp (Argus-Patch zu ServeurpersoCom/qwentts.cpp @0bf4a18)
//
// Upstream: https://github.com/ServeurpersoCom/qwentts.cpp -- MIT License
// Copyright (c) 2023-2026 The omnivoice.cpp authors
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.
//
// Aenderung ggue. Upstream: laedt optional EINE Referenzstimme (Base-Modell,
// --ref-wav/--ref-spk/--ref-rvq/--ref-text) EINMALIG beim Start und nutzt sie
// fuer ALLE Anfragen -> geklonte Stimme bei resident geladenem Modell (warm).
// Ohne Ref-Flags identisch zum Original (CustomVoice + named speaker).
#include "tts-server.h"

#include "audio-io.h"
#include "qwen.h"
#include "rvq-file.h"
#include "version.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <random>
#include <string>
#include <vector>

#ifndef _WIN32
#include <sys/mman.h>
#include <cerrno>
#endif

static const int RVQ_CODE_BITS = 11;  // 11 bits/code (V <= 2048), wie qwen-codec

static void print_usage(const char * prog) {
    fprintf(stderr, "qwentts.cpp %s\n\n", QWEN_VERSION);
    fprintf(stderr,
            "Usage: %s --model <gguf> --codec <gguf> [options]\n\n"
            "Required:\n"
            "  --model <gguf>          Talker LM GGUF (qwen-talker-*.gguf)\n"
            "  --codec <gguf>          Codec GGUF (qwen-tokenizer-*.gguf)\n\n"
            "Optional:\n"
            "  --host <ip>             Listen address (default: 127.0.0.1)\n"
            "  --port <n>              Listen port (default: 8080)\n"
            "  --lang <name>           Language label (default: auto)\n"
            "  --ref-wav <path>        Reference WAV for a cloned voice (Base model only)\n"
            "  --ref-spk <path>        Pre-extracted speaker embedding (qwen-codec)\n"
            "  --ref-rvq <path>        Pre-encoded ICL reference codes (needs --ref-spk + --ref-text)\n"
            "  --ref-text <path>       Transcript of the reference (enables ICL clone)\n"
            "  --seed <int>            Fixed sampling seed for all requests (default: -1 = random per call)\n"
            "  --temp <f>              Talker + sub-talker temperature (default: -1 = keep model default 0.9)\n"
            "  --no-fa                 Disable flash attention\n"
            "  --clamp-fp16            Clamp hidden states to FP16 range\n",
            prog);
}

static std::string basename_of(const char * path) {
    std::string s = path;
    size_t      p = s.find_last_of("/\\");
    return p == std::string::npos ? s : s.substr(p + 1);
}

// .spk: rohe f32-Werte, Anzahl == Embedding-Dimension (wie qwen-tts.cpp).
static bool read_spk_file(const char * path, std::vector<float> & emb) {
    FILE * f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[Server] ERROR: cannot open --ref-spk '%s'\n", path); return false; }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || (sz % (long) sizeof(float)) != 0) {
        fprintf(stderr, "[Server] ERROR: --ref-spk '%s' size %ld not a multiple of 4\n", path, sz);
        fclose(f);
        return false;
    }
    emb.resize((size_t) sz / sizeof(float));
    if (fread(emb.data(), sizeof(float), emb.size(), f) != emb.size()) {
        fprintf(stderr, "[Server] ERROR: short read on --ref-spk '%s'\n", path);
        fclose(f);
        return false;
    }
    fclose(f);
    return true;
}

static bool read_text_file(const char * path, std::string & out) {
    FILE * f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[Server] ERROR: cannot open '%s'\n", path); return false; }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz < 0) { fclose(f); return false; }
    out.resize((size_t) sz);
    if (sz > 0 && fread(&out[0], 1, (size_t) sz, f) != (size_t) sz) { fclose(f); return false; }
    fclose(f);
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
    return true;
}

int main(int argc, char ** argv) {
    const char *  talker_path   = NULL;
    const char *  codec_path    = NULL;
    const char *  ref_wav_path  = NULL;
    const char *  ref_spk_path  = NULL;
    const char *  ref_rvq_path  = NULL;
    const char *  ref_text_path = NULL;
    std::string   lang          = "auto";
    server_config cfg;
    bool          use_fa     = true;
    bool          clamp_fp16 = false;
    // Argus-Patch: fester Seed / Temperatur ueber alle Anfragen. seed >= 0 reseedet
    // qt_synthesize pro Request identisch -> jeder Satz startet aus demselben RNG-Zustand
    // (kein Stimm-Drift beim satzweisen Streaming). -1 == Upstream (Hardware-Random pro Call).
    int64_t       cli_seed   = -1;
    float         cli_temp   = -1.0f;

    for (int i = 1; i < argc; i++) {
        const char * arg = argv[i];
        if (!std::strcmp(arg, "--model") && i + 1 < argc) {
            talker_path = argv[++i];
        } else if (!std::strcmp(arg, "--codec") && i + 1 < argc) {
            codec_path = argv[++i];
        } else if (!std::strcmp(arg, "--host") && i + 1 < argc) {
            cfg.host = argv[++i];
        } else if (!std::strcmp(arg, "--port") && i + 1 < argc) {
            cfg.port = std::atoi(argv[++i]);
        } else if (!std::strcmp(arg, "--lang") && i + 1 < argc) {
            lang = argv[++i];
        } else if (!std::strcmp(arg, "--ref-wav") && i + 1 < argc) {
            ref_wav_path = argv[++i];
        } else if (!std::strcmp(arg, "--ref-spk") && i + 1 < argc) {
            ref_spk_path = argv[++i];
        } else if (!std::strcmp(arg, "--ref-rvq") && i + 1 < argc) {
            ref_rvq_path = argv[++i];
        } else if (!std::strcmp(arg, "--ref-text") && i + 1 < argc) {
            ref_text_path = argv[++i];
        } else if (!std::strcmp(arg, "--seed") && i + 1 < argc) {
            cli_seed = (int64_t) std::atoll(argv[++i]);
        } else if (!std::strcmp(arg, "--temp") && i + 1 < argc) {
            cli_temp = (float) std::atof(argv[++i]);
        } else if (!std::strcmp(arg, "--no-fa")) {
            use_fa = false;
        } else if (!std::strcmp(arg, "--clamp-fp16")) {
            clamp_fp16 = true;
        } else if (!std::strcmp(arg, "--help") || !std::strcmp(arg, "-h")) {
            print_usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "[CLI] ERROR: unknown arg: %s\n", arg);
            print_usage(argv[0]);
            return 1;
        }
    }

    if (!talker_path || !codec_path) {
        print_usage(argv[0]);
        return 0;
    }

    struct qt_init_params iparams;
    qt_init_default_params(&iparams);
    iparams.talker_path = talker_path;
    iparams.codec_path  = codec_path;
    iparams.use_fa      = use_fa;
    iparams.clamp_fp16  = clamp_fp16;

    struct qt_context * q = qt_init(&iparams);
    if (!q) {
        fprintf(stderr, "[Server] FATAL: %s\n", qt_last_error());
        return 1;
    }

#ifndef _WIN32
    if (mlockall(MCL_CURRENT) == 0) {
        fprintf(stderr, "[Server] Memory-pinning active (MCL_CURRENT)\n");
    } else {
        fprintf(stderr, "[Server] WARN: mlockall failed (errno=%d: %s) -- check memlock ulimit\n",
                errno, strerror(errno));
    }
#endif

    // --- Referenzstimme EINMALIG laden (Lade-Logik aus qwen-tts.cpp) ---
    std::string  ref_text_buf;
    const char * ref_text = NULL;
    if (ref_text_path) {
        if (!read_text_file(ref_text_path, ref_text_buf)) { qt_free(q); return 1; }
        if (!ref_text_buf.empty()) ref_text = ref_text_buf.c_str();
    }
    std::unique_ptr<float, void (*)(void *)> raw_holder(NULL, std::free);
    const float * ref_audio_24k = NULL;
    int           ref_n_samples = 0;
    if (ref_wav_path) {
        int     T_in = 0;
        float * raw  = audio_read_mono(ref_wav_path, 24000, &T_in);
        if (!raw || T_in <= 0) {
            fprintf(stderr, "[Server] FATAL: cannot read --ref-wav '%s'\n", ref_wav_path);
            if (raw) std::free(raw);
            qt_free(q);
            return 1;
        }
        raw_holder.reset(raw);
        ref_audio_24k = raw;
        ref_n_samples = T_in;
    }
    std::vector<float>   ref_spk_emb;
    std::vector<int32_t> ref_codes;
    int                  ref_T = 0;
    if (ref_spk_path) {
        if (!read_spk_file(ref_spk_path, ref_spk_emb)) { qt_free(q); return 1; }
        fprintf(stderr, "[Server] Reference SPK: %s, %zu f32 values\n", ref_spk_path, ref_spk_emb.size());
    }
    if (ref_rvq_path) {
        const int K = qt_num_codebooks(q);
        if (!rvq_read_file(ref_rvq_path, K, RVQ_CODE_BITS, ref_codes, &ref_T)) { qt_free(q); return 1; }
        fprintf(stderr, "[Server] Reference RVQ: %s, K=%d T=%d\n", ref_rvq_path, K, ref_T);
    }

    // Vorab in einfache, by-value capturebare Werte aufloesen. Die Buffer
    // (raw_holder, ref_text_buf/_spk_emb/_codes) leben in main und ueberdauern tts_server_run.
    const float *   cap_ref_audio = ref_audio_24k;
    const int       cap_ref_ns    = ref_n_samples;
    const char *    cap_ref_text  = ref_text;
    const float *   cap_ref_spk   = ref_spk_emb.empty() ? NULL : ref_spk_emb.data();
    const int       cap_ref_spk_d = (int) ref_spk_emb.size();
    const int32_t * cap_ref_codes = ref_codes.empty() ? NULL : ref_codes.data();
    const int       cap_ref_T     = ref_T;
    const bool      clone_mode    = (cap_ref_audio || cap_ref_spk || cap_ref_codes);
    if (clone_mode) {
        fprintf(stderr, "[Server] Voice-Clone aktiv (Referenz beim Start geladen) -- named speakers deaktiviert.\n");
    }

    tts_backend be;
    be.model_id = basename_of(talker_path);
    if (clone_mode) {
        be.voices.push_back("clone");
    } else {
        int n = qt_n_speakers(q);
        for (int i = 0; i < n; i++) {
            be.voices.push_back(qt_speaker_name(q, i));
        }
    }

    be.synthesize = [q, &lang, clone_mode, cap_ref_audio, cap_ref_ns, cap_ref_text,
                     cap_ref_spk, cap_ref_spk_d, cap_ref_codes, cap_ref_T, cli_seed, cli_temp]
                    (const tts_request & req, const tts_sink & sink, std::string & err) -> int {
        struct qt_tts_params p;
        qt_tts_default_params(&p);
        p.text = req.input.c_str();
        p.lang = lang.c_str();
        // Argus-Patch: feste Sampling-Parameter -> stabile Stimme ueber alle Saetze.
        int64_t resolved_seed = cli_seed;
        if (resolved_seed < 0) {
            std::random_device rd;
            resolved_seed = (int64_t) (((uint64_t) rd() << 32) ^ (uint64_t) rd());
            if (resolved_seed < 0) {
                resolved_seed = -resolved_seed;
            }
        }
        p.seed = resolved_seed;
        fprintf(stderr, "[Server] Synthesizing request with seed: %lld (text: \"%s\")\n",
                (long long) resolved_seed, req.input.c_str());

        if (cli_temp >= 0.0f) {
            p.temperature           = cli_temp;
            p.subtalker_temperature = cli_temp;
        }
        if (clone_mode) {
            // Geklonte Stimme statt named speaker (Base-Modell lehnt speaker/instruct ab).
            p.ref_audio_24k = cap_ref_audio;
            p.ref_n_samples = cap_ref_ns;
            p.ref_text      = cap_ref_text;
            p.ref_spk_emb   = cap_ref_spk;
            p.ref_spk_dim   = cap_ref_spk_d;
            p.ref_codes     = cap_ref_codes;
            p.ref_T         = cap_ref_T;
        } else {
            if (!req.voice.empty() && qt_n_speakers(q) > 0) {
                p.speaker = req.voice.c_str();
            }
            if (!req.instructions.empty()) {
                p.instruct = req.instructions.c_str();
            }
        }

        const tts_sink * sink_ptr = &sink;
        p.on_chunk                = [](const float * s, int ns, void * u) -> bool {
            return (*static_cast<const tts_sink *>(u))(s, ns);
        };
        p.on_chunk_user_data = (void *) sink_ptr;

        struct qt_audio out = {};
        enum qt_status  rc  = qt_synthesize(q, &p, &out);
        qt_audio_free(&out);
        if (rc != QT_STATUS_OK) {
            err = qt_last_error();
            return (int) rc;
        }
        return 0;
    };

    int rc = tts_server_run(be, cfg);
    qt_free(q);
    return rc;
}
