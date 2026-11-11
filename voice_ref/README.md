# voice_ref/

Reference voice for the cloned speech output. **Nothing in this folder is published** —
the repository ships without a voice, and `.gitignore` keeps it that way.

Put your own recording here:

| File | Content |
|---|---|
| `stimme.wav` | 10–20 seconds, one speaker, calm, no reverb. Any WAV; resampled to 24 kHz on load. |
| `stimme.txt` | the spoken text, word for word |

Then set `QWEN_CLONE_REF_WAV=/app/voice_ref/stimme.wav` and `QWEN_CLONE_REF_TEXT=` (the
spoken text) in `.env` and run `docker compose up -d tts-service`. Without these files the TTS
service uses a built-in preset voice (`QWEN_VOICE`).

Use your own voice or one you hold the rights to. Cloning another person's voice without
their consent is not acceptable, and film or broadcast audio is copyrighted on top.

---

Referenzstimme für die geklonte Sprachausgabe. **Nichts aus diesem Ordner wird
veröffentlicht** — das Repository kommt ohne Stimme, die `.gitignore` sorgt dafür.

Eigene Aufnahme hier ablegen: `stimme.wav` (10–20 Sekunden, ein Sprecher, ruhig, ohne
Hall; die Abtastrate wird beim Laden auf 24 kHz umgerechnet) und `stimme.txt` (der
gesprochene Text, Wort für Wort). Dann in der `.env`
`QWEN_CLONE_REF_WAV=/app/voice_ref/stimme.wav` und `QWEN_CLONE_REF_TEXT=` (der gesprochene
Text) setzen und `docker compose up -d tts-service` ausführen. Ohne diese Dateien spricht der Dienst mit einer
eingebauten Stimme (`QWEN_VOICE`).

Nimm deine eigene Stimme oder eine, für die du die Rechte hast. Die Stimme einer anderen
Person ohne Einwilligung zu klonen ist nicht in Ordnung, Filmton ist obendrein
urheberrechtlich geschützt.
