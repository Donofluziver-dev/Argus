import os

#--- Ohne diese Vorgabe fragt jeder Test, der Dateien liest oder die Chat-Einstellungen
#--- holt, den (in Tests nicht vorhandenen) LLM-Server nach der Bildeingabe und wartet
#--- bis zum Zeitlimit. Die Tests der Bildeingabe setzen den Schalter selbst.
os.environ.setdefault("LLM_ENABLE_MULTIMODAL", "false")
#--- Die Installation fragt sonst den Docker-Daemon nach seinen Laufzeiten (gVisor). Ein
#--- Testlauf soll nicht davon abhaengen, ob auf dem Rechner runsc installiert ist.
os.environ.setdefault("ARGUS_DOCKER_RUNTIMES", "runc")
