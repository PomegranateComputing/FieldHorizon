<#
.SYNOPSIS
  Field Horizon -- daily launcher for Windows: start, stop, status, open.

.DESCRIPTION
  start   Checks the prerequisites, refuses to start a duplicate, optionally starts the isolated
          PostgreSQL test instance (-WithLab), starts the Field Horizon service on 127.0.0.1:-Port,
          waits until it answers, then opens the interface (browser, or the desktop app with
          -Desktop). The service reconciles every DB target at start-up before accepting changes.
  stop    Asks the service to stop through its authenticated /admin/shutdown route. A running DB
          operation is not interrupted unless -Force (it is then cancelled at its next safe point,
          its change removed). Only if the service is still alive afterwards does the launcher end
          the process -- and only a process it can PROVE it started: the PID, start time and
          executable recorded in its state file must all still match.
  status  What is running, what this launcher started, and where the logs are.
  open    Opens the interface of a running service.

  Never touches a python, postgres or Ollama process it did not start. The token never appears
  in a log or on screen. Logs: %LOCALAPPDATA%\FieldHorizon\logs (one file per day for the
  launcher, one pair per start for the service).

.EXAMPLE
  .\scripts\windows\FieldHorizon.ps1                      # start (default) and open the browser
  .\scripts\windows\FieldHorizon.ps1 -Action status
  .\scripts\windows\FieldHorizon.ps1 -Action stop
  .\scripts\windows\FieldHorizon.ps1 -Action start -WithLab -Desktop
#>
param(
    [ValidateSet("start", "stop", "status", "open", "restart")] [string] $Action = "start",
    [string] $Config = "",
    [string] $Python = "",
    [int] $Port = 8777,
    [string] $TokenFile = "",
    [switch] $WithLab,
    [switch] $Desktop,
    [switch] $NoOpen,
    [switch] $Force,
    [ValidateSet("", "fr", "en")] [string] $Lang = ""
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Home_ = Join-Path $env:LOCALAPPDATA "FieldHorizon"
$LogDir = Join-Path $Home_ "logs"
$StateFile = Join-Path $Home_ "launcher-state.json"
$Settings = Join-Path $Home_ "launcher.json"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LauncherLog = Join-Path $LogDir ("launcher-{0:yyyyMMdd}.log" -f (Get-Date))

# --- settings: parameters > launcher.json > defaults for this repository ---------------------
$saved = @{}
if (Test-Path $Settings) { (Get-Content $Settings -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $saved[$_.Name] = $_.Value } }
function Pick($value, $key, $default) { if ($value) { return $value }; if ($saved[$key]) { return $saved[$key] }; return $default }
$Python = Pick $Python "python" (Join-Path $Repo "_qualification\windows\env\Scripts\python.exe")
$Config = Pick $Config "config" (Join-Path $Repo "config.yaml")
$TokenFile = Pick $TokenFile "token_file" (Join-Path (Split-Path $Config -Parent) ".fh_token")
$Lab = $saved["lab"]
if (-not $Lang) { $Lang = if ($saved["lang"]) { $saved["lang"] } elseif ((Get-UICulture).TwoLetterISOLanguageName -eq "fr") { "fr" } else { "en" } }

$Text = @{
    fr = @{
        prereq_python = "Python introuvable : {0} (environnement attendu : _qualification\windows\env ou -Python / launcher.json)."
        prereq_config = "Configuration introuvable : {0} (-Config ou launcher.json)."
        prereq_ok = "Prérequis vérifiés : Python {0}, configuration {1}."
        ollama_missing = "Ollama ne répond pas sur 127.0.0.1:11434 : les agents LLM consultatifs seront indisponibles (le reste fonctionne)."
        ollama_ok = "Ollama répond (agents LLM consultatifs disponibles)."
        already = "Field Horizon tourne déjà (PID {0}, démarré par ce lanceur le {1}). Pas de doublon."
        foreign = "Un service répond déjà sur le port {0} mais ce lanceur ne l'a pas démarré : il n'est ni réutilisé ni arrêté. Libérez le port ou utilisez -Port."
        starting = "Démarrage du service sur http://127.0.0.1:{0} …"
        started = "Service prêt (PID {0}) en {1:N1} s. Journaux : {2}"
        timeout = "Le service n'a pas répondu en {0} s. Voir {1}."
        lab_start = "Démarrage de l'instance PostgreSQL de test (laboratoire isolé) …"
        lab_running = "Instance PostgreSQL de test déjà démarrée."
        opening = "Ouverture de l'interface : {0}"
        token_hint = "Jeton d'accès : fichier {0} (jamais affiché ici)."
        stopping = "Arrêt demandé au service (route /admin/shutdown) …"
        busy = "Des opérations BDD sont en cours : arrêt refusé. Relancez avec -Force pour les annuler proprement (chacune retire son changement)."
        stopped = "Service arrêté."
        killed = "Le service ne s'est pas arrêté de lui-même : processus {0} terminé (preuve : PID, heure de démarrage et exécutable enregistrés)."
        not_ours = "Le processus enregistré n'est plus celui que ce lanceur a démarré (PID réutilisé ?) : rien n'est arrêté."
        nothing = "Aucun service démarré par ce lanceur."
        status_ours = "Service démarré par ce lanceur : PID {0}, depuis {1}, port {2}, santé HTTP {3}."
        status_none = "Aucun service démarré par ce lanceur. Port {0} : {1}."
        desktop_missing = "Application de bureau introuvable ({0}) : ouverture dans le navigateur."
    }
    en = @{
        prereq_python = "Python not found: {0} (expected environment: _qualification\windows\env, or -Python / launcher.json)."
        prereq_config = "Configuration not found: {0} (-Config or launcher.json)."
        prereq_ok = "Prerequisites checked: Python {0}, configuration {1}."
        ollama_missing = "Ollama does not answer on 127.0.0.1:11434: the advisory LLM agents will be unavailable (everything else works)."
        ollama_ok = "Ollama answers (advisory LLM agents available)."
        already = "Field Horizon is already running (PID {0}, started by this launcher at {1}). No duplicate."
        foreign = "A service already answers on port {0} but this launcher did not start it: it is neither reused nor stopped. Free the port or use -Port."
        starting = "Starting the service on http://127.0.0.1:{0} …"
        started = "Service ready (PID {0}) in {1:N1} s. Logs: {2}"
        timeout = "The service did not answer within {0} s. See {1}."
        lab_start = "Starting the isolated PostgreSQL test instance …"
        lab_running = "PostgreSQL test instance already running."
        opening = "Opening the interface: {0}"
        token_hint = "Access token: file {0} (never shown here)."
        stopping = "Asking the service to stop (/admin/shutdown) …"
        busy = "DB operations are running: stop refused. Re-run with -Force to cancel them cleanly (each removes its change)."
        stopped = "Service stopped."
        killed = "The service did not stop by itself: process {0} ended (proof: recorded PID, start time and executable)."
        not_ours = "The recorded process is no longer the one this launcher started (PID reused?): nothing is stopped."
        nothing = "No service started by this launcher."
        status_ours = "Service started by this launcher: PID {0}, since {1}, port {2}, HTTP health {3}."
        status_none = "No service started by this launcher. Port {0}: {1}."
        desktop_missing = "Desktop app not found ({0}): opening in the browser."
    }
}
function Say([string] $key, [object[]] $values = @()) {
    $message = $Text[$Lang][$key] -f $values
    Add-Content -Path $LauncherLog -Value ("{0:o} [{1}] {2}" -f (Get-Date), $Action, $message) -Encoding utf8
    Write-Host $message
}

function Get-HttpStatus([string] $Uri, [string] $Method = "Get", [hashtable] $Headers = @{}) {
    # Works on Windows PowerShell 5.1 and PowerShell 7: an HTTP error status is a result, not a failure.
    try {
        return [int](Invoke-WebRequest -Uri $Uri -Method $Method -Headers $Headers -UseBasicParsing -TimeoutSec 10).StatusCode
    } catch {
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode) { return [int]$response.StatusCode }
        return 0
    }
}

function Get-Health {
    return Get-HttpStatus "http://127.0.0.1:$Port/health"   # 401 = the service is up and requires the token
}

function Read-State {
    if (-not (Test-Path $StateFile)) { return $null }
    return Get-Content $StateFile -Raw | ConvertFrom-Json
}

function Test-OwnedProcess($state) {
    # Ours only if the PID still exists AND has the start time and executable we recorded.
    if (-not $state) { return $null }
    $process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if ($process.StartTime.ToFileTimeUtc() -ne [int64]$state.start_time -or $process.Path -ne $state.path) { return $false }
    return $process
}

function Invoke-Shutdown([bool] $force) {
    $token = (Get-Content $TokenFile -Raw).Trim()
    try {
        $uri = "http://127.0.0.1:$Port/admin/shutdown" + $(if ($force) { "?force=true" } else { "" })
        return Get-HttpStatus $uri "Post" @{ Authorization = "Bearer $token" }
    } finally { $token = $null }
}

function Open-Ui {
    $url = "http://127.0.0.1:$Port/ui/"
    if ($Desktop) {
        $app = Join-Path $Repo "apps\desktop\src-tauri\target\release\desktop.exe"
        if (Test-Path $app) { Start-Process -FilePath $app | Out-Null; Say "opening" @($app); return }
        Say "desktop_missing" @($app)
    }
    Say "opening" @($url)
    Start-Process $url | Out-Null
}

switch ($Action) {
    "status" {
        $state = Read-State
        $owned = Test-OwnedProcess $state
        if ($owned) { Say "status_ours" @($owned.Id, $owned.StartTime, $state.port, (Get-Health)) }
        else { Say "status_none" @($Port, $(if ((Get-Health) -ne 0) { "occupied" } else { "free" })) }
        Write-Host "logs: $LogDir"
        exit 0
    }
    "open" { Open-Ui; exit 0 }
    { $_ -in "stop", "restart" } {
        $state = Read-State
        $owned = Test-OwnedProcess $state
        if ($owned -eq $false) { Say "not_ours"; Remove-Item $StateFile -Force; if ($Action -eq "stop") { exit 1 } }
        elseif (-not $owned) { Say "nothing"; if ($Action -eq "stop") { exit 0 } }
        else {
            Say "stopping"
            $code = Invoke-Shutdown $Force.IsPresent
            if ($code -eq 409) { Say "busy"; exit 2 }
            $deadline = (Get-Date).AddSeconds($(if ($Force) { 150 } else { 30 }))
            while ((Get-Date) -lt $deadline -and (Get-Process -Id $owned.Id -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 500 }
            if (Get-Process -Id $owned.Id -ErrorAction SilentlyContinue) {
                # Last resort, and only for the proven process and its own children.
                & taskkill.exe /PID $owned.Id /T /F | Out-Null
                Say "killed" @($owned.Id)
            } else { Say "stopped" }
            Remove-Item $StateFile -Force -ErrorAction SilentlyContinue
        }
        if ($Action -eq "stop") { exit 0 }
    }
}

# --- start ----------------------------------------------------------------------------------
if (-not (Test-Path $Python)) { Say "prereq_python" @($Python); exit 3 }
if (-not (Test-Path $Config)) { Say "prereq_config" @($Config); exit 3 }
$pyVersion = (& $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])") 2>$null
Say "prereq_ok" @($pyVersion, $Config)
try { Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/version" -UseBasicParsing -TimeoutSec 2 | Out-Null; Say "ollama_ok" } catch { Say "ollama_missing" }

$state = Read-State
$owned = Test-OwnedProcess $state
if ($owned) { Say "already" @($owned.Id, $owned.StartTime); if (-not $NoOpen) { Open-Ui }; exit 0 }
if ((Get-Health) -ne 0) { Say "foreign" @($Port); exit 4 }

if ($WithLab -and $Lab) {
    $pgctl = Join-Path $Lab.pg_bin "pg_ctl.exe"
    & $pgctl -D $Lab.data_dir status *> $null
    if ($LASTEXITCODE -eq 0) { Say "lab_running" }
    else {
        Say "lab_start"
        & (Join-Path $Repo "scripts\dbeng\pg-test-instance.ps1") -Action start -PgBin $Lab.pg_bin -DataDir $Lab.data_dir -Port $Lab.port -CpuMask $Lab.cpu_mask | Out-Host
    }
}

$stamp = "{0:yyyyMMdd-HHmmss}" -f (Get-Date)
$out = Join-Path $LogDir "service-$stamp.out.log"   # the service's own stdout / stderr
$err = Join-Path $LogDir "service-$stamp.err.log"
Say "starting" @($Port)
# Started through ShellExecute (no -Redirect*, no -NoNewWindow): the service inherits NO handle from
# this launcher -- with CreateProcess(bInheritHandles) it would hold any pipe of the caller (a script
# capturing our output) open for its whole lifetime, the trap scripts/dbeng/pg-test-instance.ps1
# documents for pg_ctl. cmd.exe performs the redirection to the log files inside the child.
$command = '""{0}" -m fieldhorizon.cli --config "{1}" serve --port {2} --token-file "{3}" > "{4}" 2> "{5}""' -f $Python, $Config, $Port, $TokenFile, $out, $err
$process = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList @("/d", "/s", "/c", $command) -WorkingDirectory $Repo -WindowStyle Hidden -PassThru
@{ pid = $process.Id; start_time = $process.StartTime.ToFileTimeUtc(); path = $process.Path; port = $Port; config = $Config; started_at = (Get-Date).ToString("o") } |
    ConvertTo-Json | Set-Content -Path $StateFile -Encoding utf8
$t0 = Get-Date
$deadline = $t0.AddSeconds(60)
while ((Get-Date) -lt $deadline -and (Get-Health) -eq 0) {
    if ($process.HasExited) { break }
    Start-Sleep -Milliseconds 400
}
if ((Get-Health) -eq 0) { Say "timeout" @(60, $err); exit 5 }
Say "started" @($process.Id, ((Get-Date) - $t0).TotalSeconds, $LogDir)
Say "token_hint" @($TokenFile)
if (-not $NoOpen) { Open-Ui }
exit 0
