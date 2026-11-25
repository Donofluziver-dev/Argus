import os

#--- Ohne diese Vorgabe fragt jeder Test, der Dateien liest oder die Chat-Einstellungen
#--- holt, den (in Tests nicht vorhandenen) LLM-Server nach der Bildeingabe und wartet
#--- bis zum Zeitlimit. Die Tests der Bildeingabe setzen den Schalter selbst.
os.environ.setdefault("LLM_ENABLE_MULTIMODAL", "false")
