import os

# Kurzantworten ohne Modell. Sie gehen an jedes Konto gleich, deshalb ohne Namen.

FAST_PATH_DICT = {
    # KEYS bleiben ASCII-transliteriert; die VALUES gehen woertlich an den
    # Nutzer -> echte Umlaute.
    "hallo":                f"Hallo! Wie kann ich dir heute helfen?",
    "hi":                   f"Hi! Was steht an?",
    "hey":                  "Hey! Was kann ich für dich tun?",
    "guten morgen":         "Guten Morgen! Bereit für den Tag?",
    "guten tag":            "Guten Tag! Wie kann ich behilflich sein?",
    "guten abend":          f"Guten Abend! Gibt es noch etwas zu klären?",
    "wer bist du":          "Ich bin Argus, dein vielseitiger persönlicher Assistent für Technik, Alltag und Recherche.",
    "was kannst du":        "Ich helfe dir bei Coding, Texten, komplexen Analysen oder alltäglichen Fragen -- und kann eigenständig im Web recherchieren.",
    "was kannst du tun":    "Ich helfe dir bei Coding, Texten, komplexen Analysen oder alltäglichen Fragen -- und kann eigenständig im Web recherchieren.",
    "hilfe":                "Frag mich einfach nach Problemen, lass mich Texte verfassen oder nutze meine Websuche für aktuelle Infos.",
    "help":                 "Just ask about problems, let me write texts or use my web search for current info.",
    "status":               "System läuft stabil. Alle Schnittstellen (llama.cpp, Qdrant, SearxNG, SQL) sind aktiv.",
    "version":              f"Argus RAG-System. Modell: {os.getenv('OAI_MODEL', 'unbekannt')}. Stack: llama.cpp + LangGraph + Qdrant + SearxNG.",
    "danke":                f"Gern geschehen!",
    "danke dir":            "Kein Problem!",
    "danke schoen":         "Immer gerne!",
    "vielen dank":          "Immer gerne!",
    "ok":                   "Alles klar.",
    "okay":                 "Verstanden.",
    "cool":                 "Freut mich!",
    "super":                "Sehr gut. Was kommt als Naechstes?",
    "perfekt":              "Sehr gut. Was kommt als Naechstes?",
    "gut gemacht":          "Danke! Weiter geht's.",
    "alles klar":           "Alles klar. Was als Naechstes?",
    "tschuess":             f"Bis bald!",
    "bye":                  f"Bis bald!",
    "ciao":                 "Schönen Tag noch!",
    "wer hat dich erschaffen": f"Ich bin Argus, ein KI-Assistent, der lokal in deiner Organisation läuft.",
    "wie geht es dir":      "Ich bin bereit und voll einsatzfähig. Wobei darf ich dich unterstützen?",
}
