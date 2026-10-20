#!/usr/bin/env pwsh
# The Windows installer's decisions, tested without Docker or Windows.
#
# The script itself cannot be run here -- it starts containers -- so the parts
# that can silently corrupt an operator's configuration are separated out and
# checked directly. Run: pwsh tests/test_install_ps1.ps1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..' 'install.ps1')

$script:failed = 0
$script:ran = 0
function It {
    param([string]$Name, [scriptblock]$Body)
    $script:ran++
    try { & $Body; Write-Host "  ok   $Name" }
    catch { $script:failed++; Write-Host "  FAIL $Name" -ForegroundColor Red; Write-Host "       $($_.Exception.Message)" }
}
function Expect-Equal {
    param($Actual, $Expected, [string]$What = 'value')
    if ($Actual -ne $Expected) { throw "$What was [$Actual], expected [$Expected]" }
}
function Expect-True { param($Condition, [string]$What) if (-not $Condition) { throw "expected $What" } }

Write-Host 'Set-EnvLine'

It 'replaces a key in place instead of adding a second assignment' {
    # Two assignments of one key is the failure that looks like the value
    # being ignored: which one wins depends on the parser.
    $out = Set-EnvLine -Content "A=1`nPROJECTS_DIR=/old`nB=2`n" -Key 'PROJECTS_DIR' -Value '/new'
    Expect-Equal $out "A=1`nPROJECTS_DIR=/new`nB=2`n"
    Expect-Equal ([regex]::Matches($out, '(?m)^PROJECTS_DIR=').Count) 1 'assignment count'
}

It 'appends a key that is not there yet' {
    Expect-Equal (Set-EnvLine -Content "A=1`n" -Key 'B' -Value '2') "A=1`nB=2`n"
}

It 'adds a newline before appending to content that lacks one' {
    Expect-Equal (Set-EnvLine -Content 'A=1' -Key 'B' -Value '2') "A=1`nB=2`n"
}

It 'writes into empty content' {
    Expect-Equal (Set-EnvLine -Content '' -Key 'A' -Value '1') "A=1`n"
}

It 'leaves a commented-out line as documentation and adds the real assignment' {
    $out = Set-EnvLine -Content "# PROJECTS_DIR=C:\Users\you\code`n" -Key 'PROJECTS_DIR' -Value 'C:\code'
    Expect-True ($out -match '(?m)^# PROJECTS_DIR=') 'the comment to survive'
    Expect-True ($out -match '(?m)^PROJECTS_DIR=C:\\code$') 'the real assignment to be added'
}

It 'does not rewrite a different key that merely starts the same' {
    $out = Set-EnvLine -Content "PROJECTS_DIR_OLD=/keep`n" -Key 'PROJECTS_DIR' -Value '/new'
    Expect-True ($out -match '(?m)^PROJECTS_DIR_OLD=/keep$') 'the other key to be untouched'
    Expect-True ($out -match '(?m)^PROJECTS_DIR=/new$') 'the new key to be added'
}

It 'keeps a value containing = intact, because keys do' {
    $out = Set-EnvLine -Content "OPENROUTER_API_KEY=old`n" -Key 'OPENROUTER_API_KEY' -Value 'sk-or-v1-a=b=c'
    Expect-True ($out -match '(?m)^OPENROUTER_API_KEY=sk-or-v1-a=b=c$') 'the whole value to survive'
}

It 'does not let a $ in a value be read as a regex replacement group' {
    # [regex]::Replace treats $1 in a replacement STRING as a capture
    # reference; a key containing one would be silently mangled.
    $out = Set-EnvLine -Content "K=old`n" -Key 'K' -Value 'a$1b'
    Expect-True ($out -match '(?m)^K=a\$1b$') 'the literal $1 to survive'
}

Write-Host 'Format-ProjectsDir'

It 'strips the quotes a person pastes with a path' {
    Expect-Equal (Format-ProjectsDir '"C:\Users\you\code"') 'C:\Users\you\code'
}

It 'trims a trailing separator that would double up when joined' {
    Expect-Equal (Format-ProjectsDir 'C:\Users\you\code\') 'C:\Users\you\code'
    Expect-Equal (Format-ProjectsDir '/home/you/code/') '/home/you/code'
}

It 'leaves a drive root alone rather than turning it into a drive letter' {
    Expect-Equal (Format-ProjectsDir 'C:\') 'C:\'
}

Write-Host 'Test-ProjectsDirValue'

It 'accepts a Windows path' { Expect-True (Test-ProjectsDirValue -Value 'C:\Users\you\code').Ok 'accepted' }
It 'accepts a Unix path'    { Expect-True (Test-ProjectsDirValue -Value '/home/you/code').Ok 'accepted' }
It 'accepts a UNC path'     { Expect-True (Test-ProjectsDirValue -Value '\\nas\code').Ok 'accepted' }

It 'rejects a relative path, which the Docker daemon cannot resolve' {
    $v = Test-ProjectsDirValue -Value 'code'
    Expect-True (-not $v.Ok) 'rejection'
    Expect-True ($v.Reason -match 'absolute') 'a reason naming the problem'
}

It 'rejects an empty value' {
    Expect-True (-not (Test-ProjectsDirValue -Value '  ').Ok) 'rejection'
}

Write-Host 'Read-EnvValues'

It 'reads assignments and ignores comments and blank lines' {
    $v = Read-EnvValues -Content "# a comment`n`nA=1`n  B = 2  `n"
    Expect-Equal $v['A'] '1'
    Expect-Equal $v['B'] '2'
    Expect-Equal $v.Count 2 'key count'
}

It 'keeps everything after the first = in a value' {
    $v = Read-EnvValues -Content "K=a=b=c`n"
    Expect-Equal $v['K'] 'a=b=c'
}

It 'ignores a line with no =' {
    Expect-Equal (Read-EnvValues -Content "nonsense`n").Count 0 'key count'
}

Write-Host 'Explain-ComposeFailure'

# The whole point of this function is that Docker's own wording names neither
# the cause nor the remedy. Each case asserts the remedy, not the symptom.
function Explain-Of {
    param([string]$Text)
    (Explain-ComposeFailure -Output $Text 6>&1 | Out-String)
}

It 'tells a remote user to run it from their own desktop' {
    $out = Explain-Of 'error getting credentials - err: exit status 1, out: `A specified logon session does not exist.`'
    Expect-True ($out -match 'own desktop') 'the actual remedy'
    Expect-True ($out -match 'credential helper') 'what failed'
}

It 'recognises the other wording for the same failure' {
    $out = Explain-Of 'failed: logon session does not exist. It may already have been terminated.'
    Expect-True ($out -match 'own desktop') 'the same remedy'
}

It 'points at the WSL 2 engine setting when the backend is missing' {
    $out = Explain-Of 'provisioning failed: WSL 2 is not installed'
    Expect-True ($out -match 'WSL 2') 'the backend named'
    Expect-True ($out -match 'Settings') 'where to change it'
}

It 'says how much room is needed when the disk is full' {
    $out = Explain-Of 'write /var/lib/docker: no space left on device'
    Expect-True ($out -match '3 GB') 'the size it needs'
}

It 'does not call every message containing "disk" a full disk' {
    # It did. A bind mount of a missing file was reported as "out of disk
    # space", which is the failure mode this whole function is supposed to
    # prevent: confident, specific and wrong sends somebody off fixing a
    # problem they do not have.
    $out = Explain-Of 'error mounting "/host/c/x/config.yaml" to rootfs at "/app/config.yaml": not a directory'
    Expect-True (-not ($out -match '3 GB')) 'no disk-space claim'
    Expect-True ($out -match 'not there|missing') 'the real cause'
}

It 'names the two paths worth checking when a mount source is missing' {
    $out = Explain-Of 'Are you trying to mount a directory onto a file (or vice-versa)?'
    Expect-True ($out -match 'ROUTER_CONFIG') 'the router config path'
    Expect-True ($out -match 'PROJECTS_DIR') 'the projects path'
}

It 'falls back to pointing at Docker output rather than inventing a cause' {
    # Guessing wrong here is worse than saying nothing: it sends somebody off
    # fixing a problem they do not have.
    $out = Explain-Of 'something nobody has seen before'
    Expect-True ($out -match 'from Docker itself') 'an honest fallback'
    Expect-True (-not ($out -match 'credential|WSL|disk space')) 'no invented cause'
}

Write-Host ''



Write-Host 'New-DesktopShortcut'
It 'says why when the icon is missing rather than failing the install' {
    $r = New-DesktopShortcut -Url 'http://localhost:8100' -IconPath (Join-Path ([IO.Path]::GetTempPath()) 'no-such.ico') -Desktop ([IO.Path]::GetTempPath()) 6>&1
    Expect-True ($null -eq ($r | Where-Object { $_ -is [string] -and $_ -like '*.lnk' })) 'no shortcut path returned'
}
It 'the icon the installer points at ships with the checkout' {
    Expect-True (Test-Path (Join-Path $PSScriptRoot '..' 'frontend' 'public' 'persei.ico')) 'frontend/public/persei.ico exists'
}

if ($script:failed -gt 0) { Write-Host "$script:failed of $script:ran failed" -ForegroundColor Red; exit 1 }
Write-Host "$script:ran passed" -ForegroundColor Green
