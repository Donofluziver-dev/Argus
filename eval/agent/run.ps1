# Agenten-Messung starten (Windows, Stapel muss laufen).
#
#   .\eval\agent\run.ps1 -Label ist -Identity live
#   .\eval\agent\run.ps1 -Label neu -Identity eval\agent\identity\neu -Rounds 3
#   .\eval\agent\run.ps1 -Selfcheck
#   .\eval\agent\run.ps1 -Rescore -Label neu     (Pruefungen neu anwenden, ohne Modell)
#
# -Identity: "live" = die Prompt-Dateien aus <ARGUS_WORKSPACE_DIR>\identity (nur lesend),
#            "none" = ohne Prompt-Dateien (wie eine frische Installation),
#            sonst ein Ordner mit SOUL.md, SKILL.md, AGENTS.md.
# Ergebnisse: -Out (Vorgabe %TEMP%\argus_agent_eval)\<Label>\summary.txt, results.jsonl, runs\.
# Der Code kommt aus dem Arbeitsstand dieses Repos (beim Start kopiert), nicht aus dem
# gebauten Abbild.

param(
    [string]$Label = "ist",
    [string]$Identity = "live",
    [int]$Rounds = 3,
    [int]$Parallel = 2,
    # -Only a,b kommt in PowerShell als Liste an.
    [string[]]$Only = @(),
    [string[]]$Set = @(),
    [string]$Out = (Join-Path $env:TEMP "argus_agent_eval"),
    [string]$WorkspaceDir = "C:\Argus_Workspace",
    # Compose-Projekt. Leer = COMPOSE_PROJECT_NAME aus der .env, sonst "argus".
    [string]$Project = "",
    # Ordner mit llm_api_key.txt und fernet_key.txt des laufenden Stapels. Leer = API_Tokens
    # in diesem Repo. Noetig, wenn der Stapel aus einem anderen Ordner laeuft.
    [string]$SecretsDir = "",
    [switch]$Phoenix,
    [switch]$Selfcheck,
    [switch]$Rescore
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $Project) {
    # Nur diese eine Zeile lesen: in der .env stehen auch Geheimnisse.
    $envFile = Join-Path $repo ".env"
    $hit = if (Test-Path $envFile) { Select-String -Path $envFile -Pattern '^\s*COMPOSE_PROJECT_NAME=(.+)$' | Select-Object -First 1 } else { $null }
    $Project = if ($hit) { $hit.Matches[0].Groups[1].Value.Trim().Trim('"', "'") } else { "argus" }
}
$image = "$Project-rag-backend"
$fwd = { param($p) ($p -replace "\\", "/") }

if ($Selfcheck) {
    docker run --rm --network none -v "$(& $fwd "$repo\eval"):/app/eval:ro" --entrypoint python $image /app/eval/agent/run_agent_eval.py --selfcheck
    exit $LASTEXITCODE
}
if ($Rescore) {
    # Pruefungen auf die gespeicherten Laeufe von -Label neu anwenden, ohne Modell.
    docker run --rm --network none -v "$(& $fwd "$repo\eval"):/app/eval:ro" -v "$(& $fwd $Out):/out" --entrypoint python $image /app/eval/agent/run_agent_eval.py --rescore --label $Label
    exit $LASTEXITCODE
}

if (-not $SecretsDir) { $SecretsDir = Join-Path $repo "API_Tokens" }
foreach ($f in "llm_api_key.txt", "fernet_key.txt") {
    # Docker legte fuer eine fehlende Datei einen Ordner an ihrer Stelle an.
    if (-not (Test-Path (Join-Path $SecretsDir $f) -PathType Leaf)) { throw "$f fehlt in $SecretsDir (siehe -SecretsDir)" }
}

$ws = Join-Path $env:TEMP "argus_agent_eval_ws_$Label"
if (Test-Path $ws) { Remove-Item -Recurse -Force $ws }
New-Item -ItemType Directory -Force $ws, $Out | Out-Null

# Stand des Codes beim Start einfrieren: eine Messung dauert bis zu Stunden, und
# Python laedt manche Module erst spaet nach. Wer waehrenddessen weiterarbeitet,
# soll die laufende Messung nicht veraendern.
$code = Join-Path $env:TEMP "argus_agent_eval_code_$Label"
if (Test-Path $code) { Remove-Item -Recurse -Force $code }
New-Item -ItemType Directory -Force $code | Out-Null
foreach ($d in "rag_backend", "eval") {
    robocopy (Join-Path $repo $d) (Join-Path $code $d) /E /XD __pycache__ tests /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Kopieren von $d fehlgeschlagen (robocopy $LASTEXITCODE)" }
}

$mounts = @(
    "-v", "$(& $fwd "$code\rag_backend"):/app/rag_backend:ro",
    "-v", "$(& $fwd "$code\eval"):/app/eval:ro",
    "-v", "$(& $fwd "$SecretsDir\llm_api_key.txt"):/run/secrets/llm_api_key:ro",
    "-v", "$(& $fwd "$SecretsDir\fernet_key.txt"):/app/fernet_key.txt:ro",
    "-v", "$(& $fwd $ws):/host/argus_workspace",
    "-v", "$(& $fwd $Out):/out"
)
$identityArg = @()
if ($Identity -eq "live") {
    $mounts += @("-v", "$(& $fwd "$WorkspaceDir\identity"):/identity_src:ro")
    $identityArg = @("--identity", "/identity_src")
} elseif ($Identity -ne "none") {
    $src = (Resolve-Path (Join-Path $repo $Identity) -ErrorAction SilentlyContinue)
    if (-not $src) { $src = Resolve-Path $Identity }
    $mounts += @("-v", "$(& $fwd $src.Path):/identity_src:ro")
    $identityArg = @("--identity", "/identity_src")
}

$evalArgs = @("/app/eval/agent/run_agent_eval.py", "--label", $Label, "--rounds", $Rounds, "--parallel", $Parallel) + $identityArg
if ($Only) { $evalArgs += @("--only", ($Only -join ",")) }
foreach ($s in $Set) { $evalArgs += @("--set", $s) }
if ($Phoenix) { $evalArgs += "--phoenix" }

$name = "argus-agent-eval-$Label-$([guid]::NewGuid().ToString().Substring(0, 6))"
# Netz "intern" ohne Ausgang: Modell, Qdrant, embed, rerank, Phoenix. Das Web ist
# nachgebildet, die Messung kommt also nicht ins Internet.
docker create --name $name --network "${Project}_intern" @mounts --entrypoint python $image @evalArgs | Out-Null
try {
    # Rechnen und Code laufen in der Sandbox, die nur im Netz <projekt>_sandbox haengt.
    docker network connect "${Project}_sandbox" $name
    docker start -a $name
} finally {
    docker rm -f $name | Out-Null
    Remove-Item -Recurse -Force $ws, $code -ErrorAction SilentlyContinue
}
