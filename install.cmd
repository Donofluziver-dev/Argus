@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY_CMD="

rem 1. py -3 pruefen
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :run_installer
)

rem 2. python pruefen (schliesst Windows-Store-Platzhalter aus, da Testaufruf fehlschlaegt)
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :run_installer
)

rem 3. Feste Pfade einer bestehenden Installation pruefen
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set PY_CMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        goto :run_installer
    )
)
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
    "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set PY_CMD="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
        goto :run_installer
    )
)

rem 4. Python fehlt -> pruefen ob winget vorhanden ist
where winget >nul 2>&1
if errorlevel 1 (
    echo Python ab Version 3.10 wurde nicht gefunden und winget ist nicht verfuegbar.
    echo Bitte laden Sie Python ab Version 3.10 manuell herunter:
    echo https://www.python.org/downloads/
    echo.
    goto :finish
)

echo Python ab Version 3.10 wurde nicht gefunden.
echo Soll Python 3.13 jetzt ueber winget installiert werden? [J/N]
choice /c JN /n /m "Auswahl [J,N]: "
if errorlevel 2 (
    echo Installation von Python abgelehnt.
    echo Bitte laden Sie Python ab Version 3.10 manuell herunter:
    echo https://www.python.org/downloads/
    echo.
    goto :finish
)

echo Installiere Python 3.13...
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo Installation ueber winget fehlgeschlagen.
    echo Bitte laden Sie Python manuell herunter: https://www.python.org/downloads/
    echo.
    goto :finish
)

rem Nach winget-Installation: feste Pfade pruefen, da PATH in dieser Konsole nicht aktualisiert ist
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set PY_CMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        goto :run_installer
    )
)
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
    "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set PY_CMD="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
        goto :run_installer
    )
)

echo Python wurde installiert, der Interpreter konnte jedoch in dieser Konsole nicht gestartet werden.
echo Bitte oeffnen Sie ein neues Terminalfenster und starten Sie install.cmd erneut.
echo Download: https://www.python.org/downloads/
echo.
goto :finish

:run_installer
%PY_CMD% setup\install.py %*

:finish
pause
