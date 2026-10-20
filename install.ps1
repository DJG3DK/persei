#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Persei installer for Windows and macOS: the Docker bundle, set up for you.

.DESCRIPTION
    install.sh is the HOST install -- nginx, certbot, pm2, a package manager --
    and none of that exists on Windows. The bundle is the path that already
    runs anywhere Docker does, and the agent's own bind-mount translation was
    written for Windows drive letters on purpose (agent/tools/sandbox.py,
    host_path). What was missing was never the runtime. It was this: the four
    steps between a fresh copy of the source and a running console, one of
    which is `cp`, which is not a command Windows has.

    So this does what docker/README.md tells a person to do by hand. It checks
    Docker is actually usable, writes the .env, asks for the two values that
    have no sensible default, and brings the stack up.

    Nothing here is Windows-only -- it runs under PowerShell 7 on macOS and
    Linux too -- but Windows is the reason it exists.

.PARAMETER ProjectsDir
    Where your repositories live on this machine. Everything under it appears
    to the agent as /projects/<name>.

.PARAMETER OpenRouterKey
    Your OpenRouter API key. Read from the OPENROUTER_API_KEY environment
    variable when not passed.

.PARAMETER DryRun
    Print every action without performing any of it. Same idea as
    `./install.sh --dry-run`.

.PARAMETER Yes
    Never prompt. Every value must already be present, in the environment or
    as a parameter, or the run fails saying which one is missing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    Asks for what it needs and starts the stack.

    The -ExecutionPolicy is not optional advice. Windows client editions
    default to Restricted, so `.\install.ps1` on a machine nobody has changed
    fails with "running scripts is disabled on this system". Passing it on the
    command line applies to that one process and leaves the machine's own
    setting alone -- which is the right trade for a script somebody just
    downloaded.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
    Shows what it would do, touching nothing.
#>
[CmdletBinding()]
param(
    [string]$ProjectsDir,
    [string]$OpenRouterKey,
    [switch]$DryRun,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- output -----------------------------------------------------------------

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" }
function Write-Warn { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Red }

# --- the pure decisions -----------------------------------------------------
# Separated from the doing so they can be tested without Docker, a repository
# or a Windows machine -- see tests/test_install_ps1.ps1.

<#
Set one KEY=value in a .env file's text, returning the new text.

Rewrites the key in place when it is already there, so re-running never leaves
two assignments of the same name -- the later one silently wins in some
parsers and the earlier one in others, and the operator sees their value
apparently ignored. A commented-out line is left alone and the real assignment
added, because a comment is documentation, not configuration.
#>
function Set-EnvLine {
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Content,
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )
    $pattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*$'
    $line = "$Key=$Value"
    if ($Content -match $pattern) {
        return [regex]::Replace($Content, $pattern, { $line })
    }
    if ($Content.Length -gt 0 -and -not $Content.EndsWith("`n")) { $Content += "`n" }
    return $Content + $line + "`n"
}

<#
Normalise a path the operator typed for use as PROJECTS_DIR.

Strips the quotes a person pastes along with a path, and trims the trailing
separator: compose joins onto this value, and "C:\code\" would produce
"C:\code\\name".
#>
function Format-ProjectsDir {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Raw)
    $v = $Raw.Trim().Trim('"').Trim("'").Trim()
    if ($v.Length -gt 3) { $v = $v.TrimEnd('\', '/') }
    return $v
}

<#
Whether a value is usable as PROJECTS_DIR, and why not when it is not.

Relative paths are rejected rather than resolved: the value is handed to the
Docker daemon, which resolves bind mounts against its own notion of the
filesystem and not against the directory this script happened to run in.
#>
function Test-ProjectsDirValue {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return @{ Ok = $false; Reason = 'no value given' }
    }
    $isWindowsAbsolute = $Value -match '^[A-Za-z]:[\\/]'
    $isUnixAbsolute = $Value.StartsWith('/')
    $isUnc = $Value.StartsWith('\\')
    if (-not ($isWindowsAbsolute -or $isUnixAbsolute -or $isUnc)) {
        return @{ Ok = $false; Reason = "must be an absolute path (got '$Value')" }
    }
    return @{ Ok = $true; Reason = '' }
}

<#
Read KEY=value pairs out of .env text, ignoring comments and blank lines.
Used to decide what still has to be asked for on a re-run.
#>
function Read-EnvValues {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Content)
    $out = @{}
    foreach ($raw in ($Content -split "`r?`n")) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $i = $line.IndexOf('=')
        if ($i -lt 1) { continue }
        $out[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim()
    }
    return $out
}

# --- the doing --------------------------------------------------------------

$script:TOTAL_STEPS = 5

function Write-Phase {
    param([int]$N, [string]$Title)
    Write-Host ""
    Write-Host ("==> [{0}/{1}] {2}" -f $N, $script:TOTAL_STEPS, $Title) -ForegroundColor Cyan
}

<#
Run a native command, returning its combined output and exit code.

This exists because of one PowerShell rule that bites hard here: with
$ErrorActionPreference = 'Stop', ANY line a native program writes to stderr
becomes a terminating error. Docker writes its ordinary progress there --
"Image postgres:16-alpine Pulling" is stderr -- so a normal, healthy build
killed this script before it could look at the exit code, and the person saw
a PowerShell stack trace instead of the explanation below it.

Exit code is the only thing that says whether a native command failed. This
turns stderr back into text and reads that code.
#>
function Invoke-Native {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @(), [switch]$Echo)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $text = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
            $lines.Add($text)
            if ($Echo) { Write-Host $text }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return @{ Output = ($lines -join [Environment]::NewLine); ExitCode = $code }
}

function Invoke-Step {
    param([Parameter(Mandatory)][string]$Describe, [Parameter(Mandatory)][scriptblock]$Action)
    if ($DryRun) { Write-Note "would $Describe"; return $null }
    return & $Action
}

<#
A desktop shortcut to the dashboard, with the Persei logo as its icon.

The bundle has no program to launch -- the dashboard is a web page -- so a
shortcut is what a "Persei" icon on the desktop can honestly be. A .lnk
whose target is the URL opens the default browser. Replaced every run, so
a moved checkout or a changed port does not leave a dead icon behind.
Returns the shortcut path, or $null with a note when it could not be made:
the install has already succeeded at that point, and an icon is not worth
failing it over.
#>
function New-DesktopShortcut {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$IconPath,
        [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
        [string]$Name = 'Persei'
    )
    if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core' -and $env:OS -ne 'Windows_NT') {
        Write-Note 'No desktop shortcut: not Windows.'
        return $null
    }
    if (-not (Test-Path $IconPath)) {
        Write-Note "No desktop shortcut: the icon $IconPath is missing."
        return $null
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $path = Join-Path $Desktop "$Name.lnk"
        $sc = $shell.CreateShortcut($path)
        $sc.TargetPath = $Url
        $sc.IconLocation = "$IconPath,0"
        $sc.Description = 'Persei dashboard'
        $sc.Save()
        return $path
    } catch {
        Write-Note "No desktop shortcut: $($_.Exception.Message)"
        return $null
    }
}

<#
What Docker is doing right now, as one of: missing, stopped, no-compose, ready.

Split from the acting on it because "not installed" and "installed but asleep"
need completely different help, and the second is the common one: Docker
Desktop does not run at boot unless someone ticked the box.
#>
function Get-DockerState {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return 'missing' }
    if ((Invoke-Native -Exe 'docker' -Arguments @('info')).ExitCode -ne 0) { return 'stopped' }
    if ((Invoke-Native -Exe 'docker' -Arguments @('compose', 'version')).ExitCode -ne 0) { return 'no-compose' }
    return 'ready'
}

function Get-DockerDesktopPath {
    foreach ($p in @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
    )) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}

<#
Start Docker Desktop and wait for the engine, reporting progress.

Worth doing rather than telling somebody to go and start it: it is installed,
it is not running, and the whole reason they are here is that they want it
running. First start is genuinely slow -- it boots a Linux VM -- so silence
would look like a hang.
#>
function Start-DockerAndWait {
    param([int]$TimeoutSeconds = 240)
    $exe = Get-DockerDesktopPath
    if (-not $exe) {
        Write-Warn 'Docker Desktop is not where it usually installs; start it yourself and re-run.'
        return $false
    }
    Write-Note 'Starting Docker Desktop. First start boots a Linux VM, so give it a minute.'
    try { Start-Process $exe -ArgumentList '-Autostart' | Out-Null }
    catch { Write-Warn "Could not start it: $($_.Exception.Message)"; return $false }

    $waited = 0
    while ($waited -lt $TimeoutSeconds) {
        Start-Sleep -Seconds 5
        $waited += 5
        if ((Get-DockerState) -eq 'ready') {
            Write-Host ""
            Write-Note "Docker is ready (took ${waited}s)."
            return $true
        }
        Write-Host "." -NoNewline
    }
    Write-Host ""
    Write-Warn "Docker did not become ready within ${TimeoutSeconds}s."
    Write-Note 'Open Docker Desktop, wait for the whale icon to stop animating, then run this again.'
    return $false
}

<#
Turn a compose failure into something the person can act on.

Docker's own wording for the two most likely first-run failures names neither
the cause nor the remedy, and both are environmental rather than anything
wrong with this software.
#>
function Explain-ComposeFailure {
    param([string]$Output)
    if ($Output -match 'error getting credentials|logon session does not exist') {
        Write-Warn "Docker could not reach its credential helper."
        Write-Note 'This happens when Docker runs as a different Windows user than the one'
        Write-Note 'that started Docker Desktop -- over Remote Desktop, over SSH, or from a'
        Write-Note 'scheduled task. Run this installer from your own desktop instead.'
        return
    }
    if ($Output -match 'wsl|WSL 2|virtualization') {
        Write-Warn "Docker's Linux backend (WSL 2) is not available."
        Write-Note 'Open Docker Desktop > Settings > General and tick "Use the WSL 2 based'
        Write-Note 'engine", or enable virtualization in your BIOS if it is switched off.'
        return
    }
    if ($Output -match 'no space left on device|ENOSPC') {
        Write-Warn 'The machine appears to be out of disk space. The images need about 3 GB.'
        return
    }
    if ($Output -match 'mount.*(directory onto a file|not a directory)') {
        Write-Warn 'Docker could not mount a file that is not there.'
        Write-Note 'A bind mount whose source is missing makes Docker create a directory in'
        Write-Note 'its place, and the container then fails on the type mismatch. Check any'
        Write-Note 'path you set in .env -- ROUTER_CONFIG and PROJECTS_DIR are the two that'
        Write-Note 'point at something outside this folder.'
        return
    }
    Write-Note 'The output above is from Docker itself and says what it could not do.'
}

function Request-Value {
    param(
        [Parameter(Mandatory)][string]$Prompt,
        [string]$Current,
        [string]$Default,
        [switch]$Secret
    )
    if (-not [string]::IsNullOrWhiteSpace($Current)) { return $Current }
    if ($Yes) {
        if (-not [string]::IsNullOrWhiteSpace($Default)) { return $Default }
        throw "$Prompt is required, and -Yes forbids asking for it."
    }
    $shown = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
    if ($Secret) {
        $secure = Read-Host -Prompt $shown -AsSecureString
        $v = [System.Net.NetworkCredential]::new('', $secure).Password
    } else {
        $v = Read-Host -Prompt $shown
    }
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    return $v
}

function Main {
    $root = Split-Path -Parent $PSCommandPath
    Set-Location $root

    Write-Host ""
    Write-Host "  Persei installer" -ForegroundColor Cyan
    Write-Host "  Sets up the Docker bundle: the agent, its database, the model router"
    Write-Host "  and the review services, all in containers."

    Write-Phase 1 'Checking the files'
    foreach ($needed in @('docker-compose.yml', 'docker/.env.example')) {
        if (-not (Test-Path (Join-Path $root $needed))) {
            Write-Fail "$needed is missing."
            Write-Note 'Run this from inside the folder you extracted, not from beside it.'
            exit 1
        }
    }
    Write-Note "Found everything in $root"

    Write-Phase 2 'Checking Docker'
    if ($DryRun) {
        Write-Note 'would check Docker is installed, running, and has compose v2'
    } else {
        $state = Get-DockerState
        if ($state -eq 'missing') {
            Write-Fail 'Docker Desktop is not installed. It is the one thing you need first.'
            Write-Note 'Download: https://docs.docker.com/desktop/install/windows-install/'
            if (-not $Yes) {
                $open = Read-Host 'Open that page now? [Y/n]'
                if ($open -notmatch '^[Nn]') { Start-Process 'https://docs.docker.com/desktop/install/windows-install/' }
            }
            Write-Note 'Install it, start it, then run this again.'
            exit 1
        }
        if ($state -eq 'stopped') {
            Write-Note 'Docker Desktop is installed but not running.'
            if (-not (Start-DockerAndWait)) { exit 1 }
            $state = Get-DockerState
        }
        if ($state -eq 'no-compose') {
            Write-Fail 'This Docker has no `compose` command. Docker Desktop includes it;'
            Write-Note 'a very old install may not. Update Docker Desktop and run this again.'
            exit 1
        }
        Write-Note 'Docker is running.'
    }

    Write-Phase 3 'Your settings'
    $envPath = Join-Path $root '.env'
    $content = if (Test-Path $envPath) {
        Write-Note 'Keeping every value already in your .env'
        Get-Content $envPath -Raw
    } else {
        Get-Content (Join-Path $root 'docker/.env.example') -Raw
    }
    $existing = Read-EnvValues -Content $content

    $key = $OpenRouterKey
    if ([string]::IsNullOrWhiteSpace($key)) { $key = $env:OPENROUTER_API_KEY }
    if ([string]::IsNullOrWhiteSpace($key) -and $existing.ContainsKey('OPENROUTER_API_KEY')) {
        $key = $existing['OPENROUTER_API_KEY']
    }
    if ([string]::IsNullOrWhiteSpace($key) -and -not $Yes) {
        Write-Host ""
        Write-Host '  The agent calls models through OpenRouter, so it needs a key.'
        Write-Host '  Get one at https://openrouter.ai/keys -- it is the only thing that costs money.'
    }
    $key = Request-Value -Prompt 'OpenRouter API key' -Current $key -Secret

    $dir = $ProjectsDir
    if ([string]::IsNullOrWhiteSpace($dir) -and $existing.ContainsKey('PROJECTS_DIR')) {
        $dir = $existing['PROJECTS_DIR']
    }
    $suggested = Join-Path $env:USERPROFILE 'code'
    if ([string]::IsNullOrWhiteSpace($dir) -and -not $Yes) {
        Write-Host ""
        Write-Host '  Which folder should the agent work in?'
        Write-Host '  Repositories already there are visible to it, and one it clones from'
        Write-Host '  GitHub for you (from the dashboard) lands there too. Nothing outside it'
        Write-Host '  is. If everything you have is on GitHub, an empty folder is the answer.'
    }
    $dir = Format-ProjectsDir (Request-Value -Prompt 'Projects folder' -Current $dir -Default $suggested)

    $verdict = Test-ProjectsDirValue -Value $dir
    if (-not $verdict.Ok) {
        Write-Fail "That is not a usable folder: $($verdict.Reason)"
        Write-Note 'It has to be a full path, for example C:\Users\you\code'
        exit 1
    }
    if (-not $DryRun -and -not (Test-Path $dir)) {
        if ($Yes) {
            Write-Warn "$dir does not exist yet."
        } else {
            $make = Read-Host "$dir does not exist. Create it? [Y/n]"
            if ($make -notmatch '^[Nn]') {
                New-Item -ItemType Directory -Force -Path $dir | Out-Null
                Write-Note "Created $dir"
            } else {
                Write-Warn 'Leaving it. The agent will show no repositories until it exists.'
            }
        }
    }

    # Whose commits these are. Prefilled from this machine's git config when
    # it has one; blank keeps "Persei" and the sign-in address.
    $gitName = if ($existing.ContainsKey('GIT_USER_NAME')) { $existing['GIT_USER_NAME'] } else { '' }
    $gitEmail = if ($existing.ContainsKey('GIT_USER_EMAIL')) { $existing['GIT_USER_EMAIL'] } else { '' }
    if (-not $gitName -and (Get-Command git -ErrorAction SilentlyContinue)) { $gitName = (git config --global user.name 2>$null) }
    if (-not $gitEmail -and (Get-Command git -ErrorAction SilentlyContinue)) { $gitEmail = (git config --global user.email 2>$null) }
    if (-not $Yes) {
        Write-Host ""
        Write-Host '  The agent commits as you. Use the name and email your GitHub account knows.'
    }
    $gitName = Request-Value -Prompt 'Your name for commits' -Current $gitName -Default 'Persei'
    $gitEmail = Request-Value -Prompt 'Your email for commits' -Current $gitEmail -Default ''

    $content = Set-EnvLine -Content $content -Key 'OPENROUTER_API_KEY' -Value $key
    $content = Set-EnvLine -Content $content -Key 'PROJECTS_DIR' -Value $dir
    if ($gitName) { $content = Set-EnvLine -Content $content -Key 'GIT_USER_NAME' -Value $gitName }
    if ($gitEmail) { $content = Set-EnvLine -Content $content -Key 'GIT_USER_EMAIL' -Value $gitEmail }
    Invoke-Step "write $envPath" { Set-Content -Path $envPath -Value $content -NoNewline -Encoding utf8 }
    if (-not $DryRun) { Write-Note 'Saved your settings to .env' }

    Write-Phase 4 'Building and starting'
    if ($DryRun) {
        Write-Note 'would run: docker compose up -d --build'
    } else {
        Write-Note 'The first run downloads and builds about 3 GB. Ten minutes is normal.'
        Write-Note 'Docker prints its own progress below.'
        Write-Host ""
        $r = Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', '--build') -Echo
        if ($r.ExitCode -ne 0) {
            Write-Host ""
            Write-Fail 'Docker could not start the stack.'
            Explain-ComposeFailure -Output $r.Output
            exit 1
        }
    }

    Write-Phase 5 'Done'
    if ($DryRun) {
        Write-Note 'Nothing was changed. Run without -DryRun to do it for real.'
        return
    }
    $url = 'http://localhost:8100'
    Write-Host ""
    Write-Host "  Persei is running at $url" -ForegroundColor Green
    Write-Host ""
    $adminEmail = if ($existing.ContainsKey('ADMIN_EMAIL') -and $existing['ADMIN_EMAIL']) { $existing['ADMIN_EMAIL'] } else { 'admin@example.com' }
    Write-Note "Sign in as $adminEmail. The one-time password is shown by:"
    Write-Note '  docker compose exec agent python scripts/show_initial_password.py'
    Write-Note 'It works once, and you change the password on first login.'
    Write-Note 'Running this again is safe, and is also how you upgrade.'
    $icon = Join-Path $root 'frontend\public\persei.ico'
    $shortcut = Invoke-Step "put a Persei shortcut on the desktop" { New-DesktopShortcut -Url $url -IconPath $icon }
    if ($shortcut) { Write-Note "Desktop shortcut: $shortcut" }
    if (-not $Yes) {
        $open = Read-Host 'Open it in your browser now? [Y/n]'
        if ($open -notmatch '^[Nn]') { Start-Process $url }
    }
}

# Sourcing (`. ./install.ps1`) loads the functions WITHOUT installing anything,
# which is what the test file does.
if ($MyInvocation.InvocationName -ne '.') { Main }
