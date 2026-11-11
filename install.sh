#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

has_python_and_venv() {
    # venv allein reicht nicht: Debian und Ubuntu liefern ensurepip erst mit python3-venv.
    python3 -c "import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1
}

if ! has_python_and_venv; then
    echo "Python ab Version 3.10 oder das Modul 'venv' fehlt."
    if command -v apt-get >/dev/null 2>&1; then
        read -r -p "Soll python3 und python3-venv jetzt ueber apt-get installiert werden? [j/N]: " answer
        case "${answer:-}" in
            [jJ]|[jJ][aA]|[yY]|[yY][eE][sS])
                echo "Installiere python3 und python3-venv..."
                sudo apt-get update && sudo apt-get install -y python3 python3-venv
                ;;
            *)
                echo "Installation abgebrochen. Bitte Python >= 3.10 und python3-venv manuell installieren."
                exit 1
                ;;
        esac
        if ! has_python_and_venv; then
            echo "Fehler: Python >= 3.10 oder venv steht weiterhin nicht zur Verfuegung."
            exit 1
        fi
    else
        echo "Bitte installieren Sie Python >= 3.10 sowie das Modul python3-venv ueber Ihren Paketmanager."
        echo "Weitere Informationen: https://www.python.org/downloads/"
        exit 1
    fi
fi

exec python3 setup/install.py "$@"
