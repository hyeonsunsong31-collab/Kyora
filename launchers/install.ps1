<#
.SYNOPSIS
    Install Kyora's prerequisites on Windows: Python 3.12, Git, optional Node.js LTS and Claude Code.

.DESCRIPTION
    Idempotent: anything already installed is detected and left alone, so the script can be re-run.
    Uses winget for Python (Python.Python.3.12), Git (Git.Git), Node.js LTS (OpenJS.NodeJS.LTS, only
    with -WithNode) and Claude Code (Anthropic.ClaudeCode). If the Claude Code winget package cannot be
    installed, Anthropic's official installer (https://claude.ai/install.ps1) is run instead.

    Works in Windows PowerShell 5.1 and PowerShell 7. It reads and prints no secrets: Claude Code is
    used with your Claude subscription login ('claude auth login'); no API key is needed.

    Run it from the Kyora folder (the one that contains kyora.pyz):
        powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\install.ps1 [-WithNode] [-SkipClaude]

.PARAMETER WithNode
    Also install Node.js LTS (Node syntax/logic tests and the optional headless browser runner).

.PARAMETER SkipClaude
    Do not install Claude Code (for example when this PC only links to a Kyora Hub).
#>
[CmdletBinding()]
param(
    [switch]$WithNode,
    [switch]$SkipClaude
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# winget exit codes that mean "nothing to do": 0x8A150061 already installed, 0x8A15002B no applicable upgrade.
$WingetOkCodes = @(0, -1978335135, -1978335189)
$MinPython = [version]'3.10'
$Problems = New-Object System.Collections.Generic.List[string]

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    OK   $Message" -ForegroundColor Green }
function Write-Note([string]$Message) { Write-Host "    NOTE $Message" -ForegroundColor Yellow }
function Write-Bad([string]$Message) { Write-Host "    FAIL $Message" -ForegroundColor Red }

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    # winget installers update the registry PATH; copy it into this session so new tools are found.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Invoke-Native {
    # Runs a native program and captures stdout+stderr. ErrorActionPreference is relaxed inside so that
    # Windows PowerShell 5.1 does not turn stderr lines into terminating errors.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        $lines = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" })
        $code = $global:LASTEXITCODE
    } catch {
        $lines = @("$_")
        $code = -1
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ ExitCode = $code; Output = ($lines -join [Environment]::NewLine) }
}

function Get-PythonCommand {
    # Returns @{ Exe; Args; Version } for the first Python >= 3.10, or $null. The Microsoft Store
    # "python" alias (prints an install hint instead of a version) is skipped by the version parse.
    $candidates = @(
        [pscustomobject]@{ Exe = 'py'; Args = @('-3') },
        [pscustomobject]@{ Exe = 'python'; Args = @() },
        [pscustomobject]@{ Exe = 'python3'; Args = @() }
    )
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    foreach ($root in $roots) {
        foreach ($dir in @('Python313', 'Python312', 'Python311', 'Python310')) {
            $exe = Join-Path (Join-Path $root $dir) 'python.exe'
            if (Test-Path $exe) { $candidates += [pscustomobject]@{ Exe = $exe; Args = @() } }
        }
    }
    foreach ($c in $candidates) {
        if (-not (Test-Command $c.Exe) -and -not (Test-Path $c.Exe)) { continue }
        $r = Invoke-Native $c.Exe ($c.Args + @('--version'))
        if ($r.ExitCode -eq 0 -and $r.Output -match 'Python (\d+)\.(\d+)\.(\d+)') {
            $v = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
            if ($v -ge $MinPython) {
                return [pscustomobject]@{ Exe = $c.Exe; Args = $c.Args; Version = $v }
            }
        }
    }
    return $null
}

function Install-WingetPackage([string]$Id, [string]$Label) {
    $listed = Invoke-Native 'winget' @('list', '--id', $Id, '-e', '--accept-source-agreements')
    if ($listed.ExitCode -eq 0 -and $listed.Output -match [regex]::Escape($Id)) {
        Write-Ok "$Label is already installed (winget $Id)"
        return $true
    }
    Write-Host "    installing $Label with winget ($Id) ..."
    $r = Invoke-Native 'winget' @('install', '--id', $Id, '-e', '--source', 'winget', '--silent',
        '--accept-package-agreements', '--accept-source-agreements')
    Update-SessionPath
    if ($WingetOkCodes -contains $r.ExitCode) {
        Write-Ok "$Label installed (winget exit $($r.ExitCode))"
        return $true
    }
    $tail = ($r.Output -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 5) -join ' | '
    Write-Bad "winget install $Id failed (exit $($r.ExitCode)): $tail"
    return $false
}

function Add-ClaudeLocalBin {
    # The official installer puts claude.exe in %USERPROFILE%\.local\bin. Make sure it is on PATH.
    if (Test-Command 'claude') { return }
    if (-not $env:USERPROFILE) { return }
    $localBin = Join-Path $env:USERPROFILE '.local\bin'
    if (-not (Test-Path (Join-Path $localBin 'claude.exe'))) { return }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    if (($userPath -split ';') -notcontains $localBin) {
        $newPath = (@($userPath.TrimEnd(';'), $localBin) | Where-Object { $_ }) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Ok "added $localBin to your user PATH"
    }
    $env:Path = $env:Path.TrimEnd(';') + ';' + $localBin
}

function Install-ClaudeCode {
    if (Test-Command 'claude') {
        Write-Ok 'Claude Code is already installed'
        return
    }
    $viaWinget = Install-WingetPackage 'Anthropic.ClaudeCode' 'Claude Code'
    Add-ClaudeLocalBin
    if ($viaWinget -and (Test-Command 'claude')) { return }
    Write-Note 'running the official Claude Code installer from https://claude.ai/install.ps1'
    $psExe = (Get-Process -Id $PID).Path
    # Official Anthropic installer (the one the Claude Code setup documentation gives for Windows). It runs in a
    # child PowerShell so its scope, StrictMode and exit codes cannot affect this script. This is the only
    # Invoke-Expression in Kyora; scripts/ps1_lint.py enforces that. TLS 1.2 (3072) is enabled for PowerShell 5.1.
    $official = '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; irm https://claude.ai/install.ps1 | iex'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $psExe -NoProfile -ExecutionPolicy Bypass -Command $official
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    Update-SessionPath
    Add-ClaudeLocalBin
    if (-not (Test-Command 'claude')) {
        Write-Bad "Claude Code is still not on PATH (official installer exit $code)."
        $Problems.Add('Claude Code could not be installed; BUILD runs will be BLOCKED until it is installed or a Kyora Hub is linked.')
    }
}

# ------------------------------------------------------------------------------------------- main
Write-Host "Kyora prerequisites installer (PowerShell $($PSVersionTable.PSVersion))"

Write-Step 'Checking winget'
if (-not (Test-Command 'winget')) {
    Write-Bad 'winget was not found.'
    Write-Host '    Install "App Installer" from the Microsoft Store (https://apps.microsoft.com/detail/9NBLGGH4NNS1),'
    Write-Host '    or install Python 3.10+ (python.org), Git (git-scm.com) and Claude Code yourself, then re-run.'
    exit 1
}
Write-Ok 'winget is available'

Write-Step 'Python 3.10 or newer'
$py = Get-PythonCommand
if ($py) {
    Write-Ok "found Python $($py.Version) ($($py.Exe) $($py.Args -join ' '))"
} else {
    [void](Install-WingetPackage 'Python.Python.3.12' 'Python 3.12')
    $py = Get-PythonCommand
}

Write-Step 'Git'
if (Test-Command 'git') {
    Write-Ok 'Git is already installed'
} else {
    [void](Install-WingetPackage 'Git.Git' 'Git')
}

if ($WithNode) {
    Write-Step 'Node.js LTS'
    if (Test-Command 'node') {
        Write-Ok 'Node.js is already installed'
    } else {
        [void](Install-WingetPackage 'OpenJS.NodeJS.LTS' 'Node.js LTS')
    }
}

if ($SkipClaude) {
    Write-Step 'Claude Code (skipped: -SkipClaude)'
} else {
    Write-Step 'Claude Code'
    Install-ClaudeCode
}

Write-Step 'Verifying'
if ($py) {
    $r = Invoke-Native $py.Exe ($py.Args + @('--version'))
    Write-Ok "python: $($r.Output.Trim())"
} else {
    Write-Bad 'no Python 3.10+ found after installation (open a new terminal and re-run if winget just installed it)'
    $Problems.Add('Python 3.10+ is required to run kyora.pyz.')
}
if (Test-Command 'git') {
    Write-Ok "git: $((Invoke-Native 'git' @('--version')).Output.Trim())"
} else {
    Write-Bad 'git not found'
    $Problems.Add('Git is required (project slides and Claude Code''s Bash tool use it).')
}
if (Test-Command 'node') {
    Write-Ok "node: $((Invoke-Native 'node' @('--version')).Output.Trim())"
} elseif ($WithNode) {
    Write-Note 'node not found on PATH yet (open a new terminal)'
}
if (Test-Command 'claude') {
    Write-Ok "claude: $((Invoke-Native 'claude' @('--version')).Output.Trim())"
    # Only loggedIn and authMethod are read and shown; the rest of the status (account details) is not printed.
    $status = Invoke-Native 'claude' @('auth', 'status', '--json')
    $json = $null
    $text = $status.Output
    $start = $text.IndexOf('{')
    $end = $text.LastIndexOf('}')
    if ($start -ge 0 -and $end -gt $start) {
        try { $json = $text.Substring($start, $end - $start + 1) | ConvertFrom-Json } catch { $json = $null }
    }
    if ($json -and $json.PSObject.Properties['loggedIn']) {
        if ($json.loggedIn) {
            $method = ''
            if ($json.PSObject.Properties['authMethod']) { $method = " ($($json.authMethod))" }
            Write-Ok "Claude Code is logged in$method"
        } else {
            Write-Note 'Claude Code is installed but NOT logged in. Run:  claude auth login'
            Write-Note '(log in with your Claude subscription; Kyora needs no Anthropic API key)'
        }
    } else {
        Write-Note "could not read 'claude auth status --json' (older CLI?). If you have not logged in yet, run:  claude auth login"
    }
    Write-Note 'Claude Code on Windows uses Git Bash; if it cannot find bash.exe, set CLAUDE_CODE_GIT_BASH_PATH to it.'
} elseif (-not $SkipClaude) {
    Write-Bad 'claude not found'
} else {
    Write-Note 'Claude Code not installed (-SkipClaude): link a Kyora Hub in Settings, otherwise BUILD runs are BLOCKED.'
}

$launcher = Join-Path $PSScriptRoot 'kyora.ps1'
Write-Host ''
if ($Problems.Count -gt 0) {
    Write-Host 'Finished with problems:' -ForegroundColor Yellow
    foreach ($p in $Problems) { Write-Host "  - $p" -ForegroundColor Yellow }
} else {
    Write-Host 'Prerequisites are ready.' -ForegroundColor Green
}
Write-Host 'Next steps (in a NEW PowerShell window so PATH changes apply):'
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" selfcheck"
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" serve"
Write-Host '  (serve opens http://127.0.0.1:8765 in your browser)'

$pythonMissing = -not $py
$gitMissing = -not (Test-Command 'git')
if ($pythonMissing -or $gitMissing) { exit 1 }
exit 0
