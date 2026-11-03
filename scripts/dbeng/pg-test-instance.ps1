<#
.SYNOPSIS
  Field Horizon DB engineering: an isolated, disposable PostgreSQL test instance on Windows.

.DESCRIPTION
  Runs PostgreSQL from a plain binaries directory (e.g. EDB's "windows-x64-binaries" zip) as the
  current user. It installs no Windows service, needs no administrator rights and leaves any other
  PostgreSQL installation alone: the cluster lives in -DataDir, listens on 127.0.0.1:-Port only,
  and every connection needs a SCRAM-SHA-256 password.

  Secrets never go into the repository or into evidence: the superuser password is generated here
  and written to a libpq password file under -SecretsDir (default %LOCALAPPDATA%\FieldHorizon\dbeng),
  whose ACL is reduced to the current user. `field-horizon db bench-setup` adds the observer and
  operator roles to the same file.

  The instance-level settings below are part of the benchmark scenario, recorded in every
  discovery snapshot: jit and parallel query are off so that one query's timing is not a lottery
  between JIT compilation and worker start-up.

  Not settable on Windows: client_connection_check_interval ("must be set to 0 on this platform").
  The server therefore cannot notice a dead client mid-statement, so a CREATE INDEX CONCURRENTLY
  whose client was killed may still finish (a valid index nobody decided on) or leave an invalid
  one. `field-horizon db recover` handles both.

.EXAMPLE
  .\scripts\dbeng\pg-test-instance.ps1 -Action init  -PgBin C:\pg\pgsql\bin -DataDir C:\pg\cluster
  .\scripts\dbeng\pg-test-instance.ps1 -Action start -PgBin C:\pg\pgsql\bin -DataDir C:\pg\cluster
#>
param(
    [Parameter(Mandatory)] [ValidateSet("init", "start", "stop", "restart", "status", "destroy")] [string] $Action,
    [Parameter(Mandatory)] [string] $PgBin,
    [Parameter(Mandatory)] [string] $DataDir,
    [int] $Port = 55432,
    [string] $Superuser = "fh_admin",
    [string] $SecretsDir = (Join-Path $env:LOCALAPPDATA "FieldHorizon\dbeng")
)

$ErrorActionPreference = "Stop"
$PgCtl = Join-Path $PgBin "pg_ctl.exe"
$InitDb = Join-Path $PgBin "initdb.exe"
$PassFile = Join-Path $SecretsDir "pgpass.conf"
$LogFile = Join-Path (Split-Path $DataDir -Parent) "postgresql-$Port.log"

function Invoke-PgCtl([string[]] $Arguments) {
    # pg_ctl on Windows starts postgres with inherited handles: called directly from a pipeline,
    # the long-lived server keeps the caller's stdout pipe open and the caller never returns.
    # Run it with its output redirected to files and wait for pg_ctl itself only -- not for its
    # descendants, which Start-Process -Wait would also wait for.
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $PgCtl -ArgumentList $Arguments -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $process.WaitForExit()
        Get-Content $out, $err | Out-Host
        return $process.ExitCode
    } finally {
        Remove-Item $out, $err -Force -ErrorAction SilentlyContinue
    }
}

function New-Secret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    # URL-safe and free of ':' and '\', which libpq's password file treats specially.
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Protect-File([string] $Path) {
    # Owner-only: drop inherited ACEs, grant the current user full control.
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
}

function Set-PassEntry([string] $User, [string] $Secret) {
    New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
    $entry = "127.0.0.1:$($Port):*:$($User):$Secret"
    $lines = @()
    if (Test-Path $PassFile) {
        $lines = @(Get-Content $PassFile | Where-Object { $_ -notmatch "^127\.0\.0\.1:$($Port):\*:$([regex]::Escape($User)):" })
    }
    $lines += $entry
    Set-Content -Path $PassFile -Value $lines -Encoding ascii
    Protect-File $PassFile
}

switch ($Action) {
    "init" {
        if (Test-Path (Join-Path $DataDir "PG_VERSION")) { throw "A cluster already exists in $DataDir; refusing to overwrite it." }
        # A connect probe rather than Get-NetTCPConnection, which needs a CIM provider that is not
        # always available.
        $probe = New-Object System.Net.Sockets.TcpClient
        try {
            $inUse = $probe.ConnectAsync("127.0.0.1", $Port).Wait(500)
        } catch { $inUse = $false } finally { $probe.Dispose() }
        if ($inUse) { throw "Port $Port is already in use on 127.0.0.1; choose another -Port." }

        $secret = New-Secret
        $pwFile = Join-Path $SecretsDir "initdb-pw.tmp"
        New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
        Set-Content -Path $pwFile -Value $secret -Encoding ascii -NoNewline
        Protect-File $pwFile
        try {
            & $InitDb -D $DataDir -U $Superuser --pwfile=$pwFile --auth=scram-sha-256 -E UTF8 --no-locale | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "initdb failed with exit code $LASTEXITCODE" }
        } finally {
            Remove-Item $pwFile -Force -ErrorAction SilentlyContinue
        }
        Set-PassEntry $Superuser $secret

        $conf = @"

# --- Field Horizon DB engineering test instance (scripts/dbeng/pg-test-instance.ps1) ---
listen_addresses = '127.0.0.1'
port = $Port
password_encryption = 'scram-sha-256'
max_connections = 30
shared_buffers = '256MB'
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = 'all'
track_io_timing = on
jit = off
max_parallel_workers_per_gather = 0
logging_collector = off
log_line_prefix = '%m [%p] %u@%d '
"@
        Add-Content -Path (Join-Path $DataDir "postgresql.conf") -Value $conf -Encoding ascii
        # Loopback only, password always. (initdb already wrote scram-sha-256 rules; this makes the
        # file say exactly that and nothing else.)
        $hba = @"
# TYPE  DATABASE  USER  ADDRESS        METHOD
host    all       all   127.0.0.1/32   scram-sha-256
host    all       all   ::1/128        scram-sha-256
"@
        Set-Content -Path (Join-Path $DataDir "pg_hba.conf") -Value $hba -Encoding ascii
        Write-Host "Cluster initialised in $DataDir (port $Port). Password file: $PassFile"
    }
    "start" {
        $rc = Invoke-PgCtl @("-D", "`"$DataDir`"", "-l", "`"$LogFile`"", "-w", "-t", "60", "start")
        if ($rc -ne 0) { throw "pg_ctl start failed with exit code $rc (see $LogFile)" }
    }
    "stop" {
        $rc = Invoke-PgCtl @("-D", "`"$DataDir`"", "-m", "fast", "-w", "-t", "60", "stop")
        if ($rc -ne 0) { throw "pg_ctl stop failed with exit code $rc" }
    }
    "restart" {
        $rc = Invoke-PgCtl @("-D", "`"$DataDir`"", "-l", "`"$LogFile`"", "-m", "fast", "-w", "-t", "60", "restart")
        if ($rc -ne 0) { throw "pg_ctl restart failed with exit code $rc (see $LogFile)" }
    }
    "status" {
        $rc = Invoke-PgCtl @("-D", "`"$DataDir`"", "status")
        exit $rc
    }
    "destroy" {
        & $PgCtl -D $DataDir status *> $null
        if ($LASTEXITCODE -eq 0) { throw "The instance is running; stop it first." }
        if (-not (Test-Path (Join-Path $DataDir "PG_VERSION"))) { throw "$DataDir is not a PostgreSQL cluster; refusing to delete it." }
        Remove-Item -Recurse -Force $DataDir
        if (Test-Path $PassFile) {
            $kept = @(Get-Content $PassFile | Where-Object { $_ -notmatch "^127\.0\.0\.1:$($Port):" })
            if ($kept.Count -gt 0) { Set-Content -Path $PassFile -Value $kept -Encoding ascii } else { Remove-Item $PassFile -Force }
        }
        Write-Host "Destroyed cluster $DataDir and its password entries for port $Port."
    }
}
