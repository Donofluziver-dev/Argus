#Requires -RunAsAdministrator
# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    Richtet den Windows-Host so ein, dass die Argus Action-Engine PowerShell
    ausfuehren kann (Docker-Container -> OpenSSH -> Windows-Host).
.DESCRIPTION
    Einmalig als Administrator im Projektordner ausfuehren. Das Skript setzt
    ausschliesslich das, was die Action-Engine zum Arbeiten braucht:

      1. OpenSSH-Server (Feature installieren, Dienst auf Automatic, starten)
      2. Lokales Konto 'argus' + Mitgliedschaft in Administrators
      3. Ed25519-Keypair; Public-Key in administrators_authorized_keys
      4. Public-Key-Auth NUR fuer das Dienstkonto, Passwort-Auth dort AUS
      5. Arbeitsverzeichnis C:\Argus_Workspace mit Schreibrecht fuer 'argus'
      6. Firewall: Port 22 eingehend NUR aus den Docker-Subnetzen
      7. Credentials nach API_Tokens/ (owner-only ACL) -- Setup3 liest sie dort

    Alles darueber hinaus gehoert bewusst NICHT hierher.

.NOTES
    Auth-Verfahren: Public-Key (Ed25519). Der private Key liegt owner-only in
    API_Tokens/ssh_key und wird als /run/secrets/ssh_key in den rag-backend-
    Container gemountet; ein Passwort liegt dort nicht mehr. Das lokale Konto
    bleibt noetig: OpenSSH auf Windows fuehrt jeden Befehl in der Sitzung eines
    echten Kontos aus. Das Konto behaelt ein Zufallspasswort, das NIRGENDS
    gespeichert wird -- angemeldet wird sich ausschliesslich mit dem Key.

    REIHENFOLGE IST SICHERHEITSRELEVANT: Konto und Key stehen VOR der
    sshd_config-Umstellung. Wuerde 'PasswordAuthentication no' geschrieben,
    bevor der Key nachweislich registriert ist, waere die Action-Engine
    ausgesperrt. Fehlt ssh-keygen, bleibt das Skript deshalb bewusst beim
    Passwort-Verfahren, statt eine Tuer zu schliessen, die es nicht ersetzen kann.
#>

param(
    [string]$Username = "argus",
    [string]$Password = "",
    [string[]]$FirewallRemoteAddress = @("172.16.0.0/12", "192.168.65.0/24")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step { param([string]$Msg) Write-Host "`n[*] $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "    OK: $Msg" -ForegroundColor Green }
function Write-Skip { param([string]$Msg) Write-Host "    SKIP: $Msg" -ForegroundColor Yellow }
function Write-Warn { param([string]$Msg) Write-Host "    WARNUNG: $Msg" -ForegroundColor Yellow }

function New-RandomPassword {
    # Garantiert Windows-Komplexitaetsrichtlinie: Gross, Klein, Ziffer, Sonderzeichen.
    param([int]$Length = 24)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $upper  = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $lower  = 'abcdefghjkmnpqrstuvwxyz'
    $digits = '23456789'
    $special= '!@#%^&*()-_=+'
    $all    = $upper + $lower + $digits + $special

    # Unverzerrter Index 0..Max-1 via Rejection-Sampling.
    function Get-UnbiasedIndex([int]$Max) {
        $limit = [uint32]::MaxValue - ([uint32]::MaxValue % [uint32]$Max)
        do {
            $b = New-Object byte[] 4
            $rng.GetBytes($b)
            $v = [System.BitConverter]::ToUInt32($b, 0)
        } while ($v -ge $limit)
        return [int]($v % $Max)
    }

    # Je eine Pflicht-Zeichenklasse, Rest zufaellig aus dem Gesamtpool.
    $chars = [System.Collections.Generic.List[char]]::new()
    foreach ($pool in @($upper, $lower, $digits, $special)) {
        $chars.Add($pool[(Get-UnbiasedIndex $pool.Length)])
    }
    for ($i = 4; $i -lt $Length; $i++) {
        $chars.Add($all[(Get-UnbiasedIndex $all.Length)])
    }
    # Fisher-Yates-Shuffle (kryptografisch, unverzerrt)
    for ($i = $chars.Count - 1; $i -gt 0; $i--) {
        $j = Get-UnbiasedIndex ($i + 1)
        $tmp = $chars[$i]; $chars[$i] = $chars[$j]; $chars[$j] = $tmp
    }
    $rng.Dispose()
    -join $chars
}

function Set-OwnerOnlyAcl {
    param([string]$Path)
    try {
        $acl = Get-Acl -Path $Path
        $acl.SetAccessRuleProtection($true, $false)
        $acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
        $owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $owner, "FullControl", "Allow"
        )
        $acl.AddAccessRule($rule)
        Set-Acl -Path $Path -AclObject $acl
    } catch {
        Write-Warn "ACL-Setzung fuer $Path fehlgeschlagen: $_"
    }
}

function Set-SshdKeyFileAcl {
    <#
      ACL fuer administrators_authorized_keys. sshd prueft diese Datei streng:
      keine Vererbung, Schreibrecht ausschliesslich fuer SYSTEM und die
      Administrators-Gruppe, Owner ebenfalls einer von beiden. Stimmt etwas
      nicht, IGNORIERT sshd die Datei still -- die Anmeldung scheitert dann mit
      "Permission denied (publickey)" und keiner Zeile im Log, die den Grund nennt.
      Principals ueber well-known SIDs, nicht ueber lokalisierte Namen
      (S-1-5-18 = SYSTEM, S-1-5-32-544 = Administrators).
    #>
    param([string]$Path)
    try {
        $acl = Get-Acl -Path $Path
        $acl.SetAccessRuleProtection($true, $false)
        $acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
        foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
            $id = New-Object System.Security.Principal.SecurityIdentifier($sid)
            $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
                $id, "FullControl", "Allow")))
        }
        $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")))
        Set-Acl -Path $Path -AclObject $acl
        return $true
    } catch {
        Write-Warn "ACL-Setzung fuer $Path fehlgeschlagen: $_"
        return $false
    }
}

function Resolve-WellKnownGroup {
    <#
      Loest eine Gruppe ueber ihre well-known SID auf, statt ueber den lokalisierten
      Namen. Vorher stand hier eine feste Liste ("Administratoren","Administrators",
      ...) -- auf einem franzoesischen, spanischen oder italienischen Windows griff
      davon kein einziger Eintrag, der argus-User landete in KEINER Admin-Gruppe und
      die Action-Engine scheiterte spaeter still an jedem Host-Befehl. Die SID ist
      sprachunabhaengig.
    #>
    param([string]$Sid)
    try {
        return (New-Object System.Security.Principal.SecurityIdentifier($Sid)).Translate(
            [System.Security.Principal.NTAccount]).Value.Split('\')[-1]
    } catch {
        return $null
    }
}

#--- 0. Credentials (API_Tokens/) -- Setup3 und der rag-backend-Mount lesen sie hier
Write-Step "Credentials in API_Tokens/ vorbereiten"
$apiDir = Join-Path $PSScriptRoot "API_Tokens"
if (-not (Test-Path $apiDir)) {
    New-Item -ItemType Directory -Path $apiDir -Force | Out-Null
}
$userFile = Join-Path $apiDir "ssh_user.txt"
$passFile = Join-Path $apiDir "ssh_password.txt"
$keyFile  = Join-Path $apiDir "ssh_key"
$pubFile  = "$keyFile.pub"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

# Zustand VOR allen Aenderungen festhalten -- $Password wird gleich ueberschrieben.
$passwordProvided = [bool]$Password
$keyExistedAtStart = (Test-Path $keyFile) -and ((Get-Item $keyFile).Length -gt 0)

# Username bestimmen (idempotent: bestehende ssh_user.txt hat Vorrang).
if (Test-Path $userFile) {
    $savedUser = ([System.IO.File]::ReadAllText($userFile, $utf8NoBom)).TrimStart([char]0xFEFF).Trim()
    if ($savedUser -ne "" -and $savedUser -ne $Username) {
        Write-Skip "ssh_user.txt existiert mit '$savedUser' -- Parameter-Username '$Username' wird ignoriert."
        $Username = $savedUser
    }
}
[System.IO.File]::WriteAllText($userFile, $Username, $utf8NoBom)
Set-OwnerOnlyAcl -Path $userFile
Write-Ok "SSH_USER geschrieben: $userFile"

# Passwort bestimmen. Gespeichert wird es nur im Passwort-Fallback (Schritt 5).
if (-not $Password) {
    if (Test-Path $passFile) {
        $Password = ([System.IO.File]::ReadAllText($passFile, $utf8NoBom)).TrimStart([char]0xFEFF).Trim()
        if ($Password) {
            Write-Skip "SSH_PASSWORD bereits vorhanden -- bestehender Wert wird verwendet."
        }
    }
    if (-not $Password) {
        $Password = New-RandomPassword -Length 24
        Write-Ok "Neues Konto-Passwort generiert."
    }
} else {
    Write-Ok "Konto-Passwort via -Password-Parameter uebergeben."
}

# Die Passwort-DATEI entsteht erst in Schritt 5 -- erst dann steht fest, ob das
# Konto das Passwort uebernommen hat und ob der Key-Pfad zustande kam.

#--- 1. OpenSSH-Server installieren
Write-Step "Pruefe OpenSSH-Server Installation"
$sshService = Get-Service -Name sshd -ErrorAction SilentlyContinue
if (-not $sshService) {
    Write-Ok "OpenSSH-Server wird installiert (dies kann einige Minuten dauern)..."

    # Temporaerer WSUS-Bypass, nur wenn die Policy gesetzt ist (sonst 0x800f0954).
    $wuPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    $wsusBypassed = $false
    try {
        if (Test-Path $wuPath) {
            $val = Get-ItemProperty -Path $wuPath -Name "UseWUServer" -ErrorAction SilentlyContinue
            if ($val -and $val.UseWUServer -eq 1) {
                Write-Ok "WSUS aktiv: Umgehe Windows-Update-Server temporaer fuer Direkt-Download..."
                Set-ItemProperty -Path $wuPath -Name "UseWUServer" -Value 0
                Restart-Service -Name wuauserv -Force -ErrorAction SilentlyContinue | Out-Null
                $wsusBypassed = $true
            }
        }
    } catch {
        Write-Warn "WSUS-Bypass fehlgeschlagen: $_"
    }

    try {
        # Add-WindowsCapability hat kein .State-Feld -- nicht darauf zugreifen.
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
        $capState = (Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0).State
        if ($capState -eq "Installed") {
            Write-Ok "OpenSSH-Server erfolgreich installiert."
        } else {
            Write-Warn "Installation beendet mit Status: $capState"
        }
    } catch {
        Write-Warn "Fehler bei Add-WindowsCapability: $_"
        Write-Host "Versuche alternativen DISM-Aufruf..." -ForegroundColor Yellow
        & dism.exe /online /add-capability /capabilityname:OpenSSH.Server~~~~0.0.1.0
    } finally {
        if ($wsusBypassed) {
            Write-Ok "Restauriere WSUS-Server Einstellungen..."
            try {
                Set-ItemProperty -Path $wuPath -Name "UseWUServer" -Value 1
                Restart-Service -Name wuauserv -Force -ErrorAction SilentlyContinue | Out-Null
            } catch {
                Write-Warn "Konnte WSUS-Einstellung nicht restaurieren."
            }
        }
    }
} else {
    Write-Ok "OpenSSH-Server ist bereits installiert."
}

#--- 2. SSH-Dienst konfigurieren & starten
Write-Step "Konfiguriere OpenSSH-Dienst"
Set-Service -Name sshd -StartupType Automatic
$sshService = Get-Service -Name sshd
if ($sshService.Status -ne "Running") {
    Start-Service -Name sshd
    Write-Ok "sshd gestartet."
} else {
    Write-Ok "sshd laeuft bereits."
}

#--- 3. Lokaler User 'argus'
Write-Step "Lokaler User '$Username' einrichten"
$existingUser = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
$securePass = ConvertTo-SecureString $Password -AsPlainText -Force
if ($existingUser) {
    # -PasswordNeverExpires auch am bestehenden Konto: ein abgelaufenes Passwort
    # verhindert auch die Key-Anmeldung.
    if ($keyExistedAtStart -and -not $passwordProvided) {
        # Laeuft die Anmeldung ueber den Key, das Passwort NICHT neu wuerfeln.
        Set-LocalUser -Name $Username -PasswordNeverExpires $true
        Write-Skip "Key vorhanden -- Konto-Passwort bleibt unveraendert (laeuft nicht ab)."
    } else {
        Set-LocalUser -Name $Username -Password $securePass -PasswordNeverExpires $true
        Write-Ok "Passwort fuer '$Username' aktualisiert (laeuft nicht ab)."
    }
} else {
    New-LocalUser -Name $Username -Password $securePass -PasswordNeverExpires -AccountNeverExpires -Description "SSH Service-Account fuer Argus Action-Engine" | Out-Null
    Write-Ok "User '$Username' erstellt."
}
Enable-LocalUser -Name $Username -ErrorAction SilentlyContinue

#--- 4. Administrators-Mitgliedschaft
#--- Nur diese eine Gruppe: die Action-Engine fuehrt Admin-PowerShell aus (Updates,
#--- Dienste, Eventlog). 'Remote Management Users' stand hier frueher zusaetzlich --
#--- das war eine WinRM-Altlast und fuer OpenSSH ohne Wirkung.
Write-Step "Gruppenmitgliedschaft (Administrators)"
$adminGroup = Resolve-WellKnownGroup "S-1-5-32-544"
$adminGroupJoined = $false
if (-not $adminGroup) {
    Write-Warn "Administrators-Gruppe konnte nicht ueber SID S-1-5-32-544 aufgeloest werden."
} else {
    try {
        $members = Get-LocalGroupMember -Group $adminGroup -ErrorAction SilentlyContinue |
                   Select-Object -ExpandProperty Name
        if ($members -notcontains "$env:COMPUTERNAME\$Username") {
            Add-LocalGroupMember -Group $adminGroup -Member $Username -ErrorAction Stop
            Write-Ok "${adminGroup}: hinzugefuegt."
        } else {
            Write-Skip "${adminGroup}: bereits Mitglied."
        }
    } catch {
        if ($_.Exception.Message -like "*already a member*" -or $_.Exception.Message -like "*bereits*") {
            Write-Skip "${adminGroup}: bereits Mitglied."
        } else {
            Write-Warn "Fehler beim Hinzufuegen zu '${adminGroup}': $($_.Exception.Message)"
        }
    }
    # Mitgliedschaft nachweisen: ohne sie scheitert jeder Host-Befehl still.
    $verify = Get-LocalGroupMember -Group $adminGroup -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty Name
    if ($verify -contains "$env:COMPUTERNAME\$Username") { $adminGroupJoined = $true }
}
if (-not $adminGroupJoined) {
    Write-Warn "KRITISCH: '$Username' ist nicht in der Administrators-Gruppe. Host-Befehle werden fehlschlagen!"
}

#--- 5. Ed25519-Keypair + administrators_authorized_keys
#--- $keyReady wird nur $true, wenn Keypair UND Eintrag UND ACL stehen.
Write-Step "SSH-Keypair (Ed25519) fuer die Action-Engine"
$keyReady = $false
$adminKeysPath = Join-Path $env:ProgramData "ssh\administrators_authorized_keys"

$keygen = Join-Path $env:SystemRoot "System32\OpenSSH\ssh-keygen.exe"
if (-not (Test-Path $keygen)) {
    $keygenCmd = Get-Command "ssh-keygen.exe" -ErrorAction SilentlyContinue
    $keygen = if ($keygenCmd) { $keygenCmd.Source } else { $null }
}

if (-not $keygen) {
    Write-Warn "ssh-keygen nicht gefunden (OpenSSH-Client-Feature fehlt)."
    Write-Warn "Das Skript bleibt beim Passwort-Verfahren. Fuer den Key-Umstieg:"
    Write-Warn "  Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
} else {
    $haveKey = (Test-Path $keyFile) -and ((Get-Item $keyFile).Length -gt 0) -and (Test-Path $pubFile)
    if ($haveKey) {
        Write-Skip "Keypair existiert bereits -- wird wiederverwendet."
    } else {
        # Vorhandene Dateien wegraeumen: ssh-keygen fragt sonst interaktiv nach.
        Remove-Item -Path $keyFile, $pubFile -Force -ErrorAction SilentlyContinue

        # Leere Passphrase -- Schreibweise unterscheidet sich je PowerShell-Version.
        $emptyPass = if ($PSVersionTable.PSVersion.Major -ge 6) { '' } else { '""' }
        & $keygen -t ed25519 -f $keyFile -C "argus-action-engine" -q -N $emptyPass
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pubFile)) {
            Write-Warn "ssh-keygen fehlgeschlagen (Exit $LASTEXITCODE) -- Passwort-Verfahren bleibt aktiv."
        } else {
            Write-Ok "Keypair erzeugt: $keyFile"
        }
    }

    if ((Test-Path $keyFile) -and (Test-Path $pubFile)) {
        Set-OwnerOnlyAcl -Path $keyFile
        $pubContent = ([System.IO.File]::ReadAllText($pubFile, $utf8NoBom)).TrimStart([char]0xFEFF).Trim()
        if (-not $pubContent) {
            Write-Warn "Public-Key-Datei ist leer -- Passwort-Verfahren bleibt aktiv."
        } else {
            # sshd liest bei Administrators-Konten AUSSCHLIESSLICH
            # administrators_authorized_keys, nicht das Profilverzeichnis.
            $keptLines = @()
            if (Test-Path $adminKeysPath) {
                $keptLines = @([System.IO.File]::ReadAllLines($adminKeysPath)) |
                    Where-Object { $_.Trim() -ne "" -and $_ -notmatch 'argus-action-engine\s*$' }
            } else {
                New-Item -ItemType File -Path $adminKeysPath -Force | Out-Null
            }
            # Alte argus-Keys ersetzen, fremde Eintraege behalten.
            $allLines = @($keptLines) + @($pubContent)
            # BOM-frei schreiben -- sshd stolpert sonst ueber die erste Zeile.
            [System.IO.File]::WriteAllLines($adminKeysPath, [string[]]$allLines, $utf8NoBom)
            if (Set-SshdKeyFileAcl -Path $adminKeysPath) {
                $keyReady = $true
                Write-Ok "Public-Key eingetragen: $adminKeysPath"
                Write-Ok "ACL gesetzt (nur SYSTEM + Administrators)."
            } else {
                Write-Warn "ACL nicht setzbar -- sshd wuerde die Datei ignorieren. Passwort-Verfahren bleibt aktiv."
            }
        }
    }
}

#--- 6. sshd_config: Auth-Verfahren fuer das Dienstkonto festlegen
Write-Step "sshd_config: Auth-Verfahren fuer '$Username'"
$sshdConfigPath = "C:\ProgramData\ssh\sshd_config"
if (Test-Path $sshdConfigPath) {
    # BOM-frei lesen und schreiben.
    $utf8NoBomCfg = New-Object System.Text.UTF8Encoding $false
    $raw = [System.IO.File]::ReadAllText($sshdConfigPath, $utf8NoBomCfg)
    $raw = $raw.TrimStart([char]0xFEFF)

    # Match-Block ans Dateiende statt globalem Flag: gilt nur fuer '$Username'.
    # Match-Bloecke wirken bis zum naechsten Match oder EOF.
    $marker = "# Argus-ActionEngine: Auth-Verfahren fuer das argus-Dienstkonto (Match-Block)"
    if ($keyReady) {
        $authLines = @(
            "    PubkeyAuthentication yes",
            "    PasswordAuthentication no",
            # Ohne KbdInteractiveAuthentication no bleibt der Passwortweg offen.
            "    KbdInteractiveAuthentication no",
            # AuthorizedKeysFile explizit: bei mehreren Match-Bloecken gewinnt
            # der erste Wert je Direktive.
            "    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys"
        )
    } else {
        # Fallback: ohne registrierten Key bleibt Passwort-Auth die einzige Tuer.
        $authLines = @(
            "    PubkeyAuthentication yes",
            "    PasswordAuthentication yes"
        )
    }
    $matchBlock = (@($marker, "Match User $Username") + $authLines) -join "`r`n"

    if ($raw -match [regex]::Escape($matchBlock)) {
        Write-Skip "sshd_config enthaelt bereits genau diesen Argus-Block."
    } else {
        # Alten Argus-Block entfernen statt danebenzuschreiben -- der erste Wert
        # je Direktive gewinnt.
        $body = [regex]::Replace($raw, "(?ms)\r?\n*^# Argus-ActionEngine:.*\z", "")
        $body = $body.TrimEnd("`r", "`n")
        $newContent = "$body`r`n`r`n$matchBlock`r`n"
        [System.IO.File]::WriteAllText($sshdConfigPath, $newContent, $utf8NoBomCfg)

        # Config vor dem Neustart validieren.
        $sshdExe = Join-Path $env:SystemRoot "System32\OpenSSH\sshd.exe"
        $configOk = $true
        if (Test-Path $sshdExe) {
            $testOutput = & $sshdExe -t -f $sshdConfigPath 2>&1
            if ($LASTEXITCODE -ne 0) {
                $configOk = $false
                [System.IO.File]::WriteAllText($sshdConfigPath, $raw, $utf8NoBomCfg)
                Write-Warn "sshd_config-Validierung fehlgeschlagen -- Aenderung zurueckgenommen:"
                $testOutput | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
                Write-Warn "Die Action-Engine kann sich so nicht anmelden. sshd_config pruefen und Skript erneut starten."
            }
        } else {
            Write-Warn "sshd.exe nicht unter $sshdExe gefunden -- Config wird ohne Validierung uebernommen."
        }

        if ($configOk) {
            Restart-Service -Name sshd -Force
            if ($keyReady) {
                Write-Ok "Public-Key-Auth fuer '$Username' aktiv, Passwort-Auth dort abgeschaltet. sshd neu gestartet."
            } else {
                Write-Ok "Passwort-Auth auf '$Username' begrenzt (Match-Block am Dateiende, BOM-frei), sshd neu gestartet."
            }
        }
    }
} else {
    Write-Warn "sshd_config nicht unter $sshdConfigPath gefunden! Standardwerte werden angenommen."
}

#--- 7. Passwort-Datei: nur im Fallback befuellen
Write-Step "Passwort-Datei in API_Tokens/"
if ($keyReady) {
    # Leeren statt loeschen: fehlt die Datei beim 'up', legt Docker an ihrer
    # Stelle ein VERZEICHNIS an.
    [System.IO.File]::WriteAllText($passFile, "", $utf8NoBom)
    Set-OwnerOnlyAcl -Path $passFile
    Write-Ok "Passwort-Datei geleert -- im Container liegt nur noch der Key."
    Write-Host "    Das Konto-Passwort wird NIRGENDS gespeichert. Brauchst du eines" -ForegroundColor DarkGray
    Write-Host "    (z. B. zum manuellen Anmelden), setze es mit -Password neu." -ForegroundColor DarkGray
} else {
    [System.IO.File]::WriteAllText($passFile, $Password, $utf8NoBom)
    Set-OwnerOnlyAcl -Path $passFile
    Write-Ok "SSH_PASSWORD persistiert (Fallback-Verfahren): $passFile"
}

#--- 8. Arbeitsverzeichnis C:\Argus_Workspace
#--- Temp = Arbeitsdateien der Action-Engine, Uploads = eingehend,
#--- Outbound = herausgegebene Ergebnisse. Zugleich :rw-Mount des rag-backend.
Write-Step "C:\Argus_Workspace anlegen und berechtigen"
$workspaceDir = "C:\Argus_Workspace"
foreach ($sub in @($workspaceDir,
                   (Join-Path $workspaceDir "Temp"),
                   (Join-Path $workspaceDir "Uploads"),
                   (Join-Path $workspaceDir "Outbound"))) {
    if (-not (Test-Path $sub)) {
        New-Item -ItemType Directory -Path $sub -Force | Out-Null
    }
}

try {
    $argusSid = (New-Object System.Security.Principal.NTAccount($Username)).Translate(
        [System.Security.Principal.SecurityIdentifier])
    $acl = Get-Acl -Path $workspaceDir
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $argusSid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $workspaceDir -AclObject $acl
    Write-Ok "Workspace angelegt: $workspaceDir (argus hat FullControl)."
} catch {
    Write-Warn "ACL-Setzung fuer Workspace fehlgeschlagen: $_"
}

#--- 9. Windows-Firewall: Port 22 nur aus den Docker-Subnetzen
Write-Step "Firewall-Regel fuer OpenSSH"
$ruleName = "SSH-Docker-ActionEngine"
$sshPort = 22
$existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existingRule) {
    Set-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Enabled True -Profile Any | Out-Null
    $existingRule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $sshPort | Out-Null
    $existingRule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $FirewallRemoteAddress | Out-Null
    Write-Ok "Regel '$ruleName' aktualisiert (TCP Port 22, RemoteAddress: $($FirewallRemoteAddress -join ', '))."
} else {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Description "Erlaubt SSH-Zugriff von Docker-Containern (Action-Engine)" `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $sshPort `
        -RemoteAddress $FirewallRemoteAddress `
        -Action Allow `
        -Profile Any | Out-Null
    Write-Ok "Firewall-Regel erstellt (TCP Port 22, RemoteAddress: $($FirewallRemoteAddress -join ', '))."
}

#--- Windows-Default-Regel des OpenSSH-Features deaktivieren: sie erlaubt Port 22
#--- von jeder Adresse und hebelt die RemoteAddress-Beschraenkung aus.
$defaultSshRules = Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -like "OpenSSH-Server-In-TCP*" -or $_.DisplayName -eq "OpenSSH SSH Server (sshd)") `
    -and $_.DisplayName -ne $ruleName
}
foreach ($dr in $defaultSshRules) {
    if ($dr.Enabled -eq "True") {
        try {
            Disable-NetFirewallRule -Name $dr.Name -ErrorAction Stop
            Write-Ok "Default-Regel '$($dr.DisplayName)' deaktiviert (erlaubte Port 22 von ueberall -- nur die Docker-Regel bleibt aktiv)."
        } catch {
            Write-Warn "Konnte Default-Regel '$($dr.DisplayName)' nicht deaktivieren: $_"
        }
    } else {
        Write-Skip "Default-Regel '$($dr.DisplayName)' bereits deaktiviert."
    }
}

#--- 10. Lokaler Lauschtest (mit Timeout, damit ein gefilterter Port 22 nicht haengt)
Write-Step "Lauscht sshd? (Loopback-Test)"
$t = $null
try {
    $t = New-Object System.Net.Sockets.TcpClient
    $connectTask = $t.ConnectAsync("127.0.0.1", 22)
    if ($connectTask.Wait(3000)) {
        if ($t.Connected) {
            Write-Ok "sshd nimmt Verbindungen auf Port 22 an."
        } else {
            Write-Warn "TCP-Verbindung fehlgeschlagen (kein Fehler, aber nicht verbunden)."
        }
    } else {
        Write-Warn "Timeout nach 3s. Vergewissere dich, dass sshd laeuft."
    }
} catch {
    Write-Warn "Loopback-Test fehlgeschlagen: $_. Vergewissere dich, dass sshd laeuft."
} finally {
    # Auch im Fehlerfall freigeben -- vorher blieb der Socket bei jeder Exception offen.
    if ($t) { $t.Dispose() }
}
# Loopback umgeht die Windows-Firewall -- die Docker-Regel ist damit nicht geprueft.
Write-Host "    Hinweis: Loopback umgeht die Firewall -- die Docker-Regel ist damit NICHT geprueft." -ForegroundColor DarkGray

Write-Host "`n" -NoNewline
Write-Host "======================================" -ForegroundColor Green
Write-Host " Host-Setup abgeschlossen" -ForegroundColor Green
Write-Host "======================================" -ForegroundColor Green
Write-Host ""
if ($keyReady) {
    Write-Host "Auth-Verfahren: Public-Key (Ed25519)" -ForegroundColor Green
    Write-Host "Credentials:"
    Write-Host "       $userFile"
    Write-Host "       $keyFile        (privat, owner-only)"
    Write-Host "       $adminKeysPath  (public)"
} else {
    Write-Host "Auth-Verfahren: Passwort (Key-Setup nicht moeglich)" -ForegroundColor Yellow
    Write-Host "Credentials:"
    Write-Host "       $userFile"
    Write-Host "       $passFile"
}
Write-Host ""
Write-Host "Naechste Schritte:"
Write-Host "  1. python Setup3.py            (liest die Credentials, generiert die Action-Engine)"
Write-Host "  2. docker compose up -d --build"
Write-Host "  3. Echten Pfad testen -- prueft Firewall-Regel UND Key-Anmeldung in einem:"
Write-Host "     docker compose exec rag-backend python -c ""import asyncio; from rag_backend.action_engine.ssh_client import run_powershell as r; print(asyncio.run(r('whoami')))"""
Write-Host "     Erwartete Ausgabe: {'stdout': '<rechner>\$Username', ...}"
Write-Host ""
