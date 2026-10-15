<#
.SYNOPSIS
    Start Kyora on Windows.

.DESCRIPTION
    Usage:  kyora.ps1 [serve|hub|selfcheck|bench|version] [args...]

      serve       run the local Kyora server and open the UI in your browser (default command;
                  add --no-open to skip the browser)
      hub         serve and expose the code engine to phones/tablets on your LAN (prints link + QR)
      selfcheck   check Nemotron, Kimi K3, Claude Code, Node, Git and Tokkio reachability
      bench       Kyora Bench (real pipeline runs; costs NVIDIA tokens and Claude usage)
      version     print the Kyora version

    Every other argument is passed to Kyora unchanged, e.g.  kyora.ps1 serve --port 9000

    Release layout: <root>\kyora.pyz and <root>\launchers\kyora.ps1 -> runs "python <root>\kyora.pyz".
    Source checkout (no kyora.pyz): runs "python -m kyora" with <root> on PYTHONPATH.
    KYORA_HOME (runtime data: projects, runs, logs) defaults to %USERPROFILE%\.kyora.
    The script exits with Kyora's exit code and never prints keys or tokens itself.

    If scripts are blocked by the execution policy, start it as:
        powershell -NoProfile -ExecutionPolicy Bypass -File .\launchers\kyora.ps1 serve
#>
param()
# No declared parameters on purpose: everything is read from $args so that Kyora's --long-options
# (for example --port 9000) reach Kyora untouched instead of being parsed by PowerShell.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Commands = @('serve', 'hub', 'selfcheck', 'bench', 'version')
$MinPython = [version]'3.10'

function Show-Usage {
    Write-Host 'Usage: kyora.ps1 [serve|hub|selfcheck|bench|version] [args...]'
    Write-Host '  serve      local server + UI (opens the browser; --no-open to skip)'
    Write-Host '  hub        serve + expose the code engine on the LAN (link + QR for phones)'
    Write-Host '  selfcheck  check models, engines, tools and Tokkio'
    Write-Host '  bench      Kyora Bench (real runs; costs tokens)'
    Write-Host '  version    print the version'
}

function Find-Python {
    # First Python >= 3.10: the py launcher, python, python3, then the usual per-user / all-users installs.
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
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue) -and -not (Test-Path $c.Exe)) { continue }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $out = (& $c.Exe @($c.Args + @('--version')) 2>&1 | ForEach-Object { "$_" }) -join ' '
        } catch {
            $out = ''
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($out -match 'Python (\d+)\.(\d+)') {
            if ([version]("{0}.{1}" -f $Matches[1], $Matches[2]) -ge $MinPython) { return $c }
        }
    }
    return $null
}

# ------------------------------------------------------------------------------------------- arguments
$all = @($args)
$command = 'serve'
$rest = @()
if ($all.Count -gt 0) {
    $command = [string]$all[0]
    if ($all.Count -gt 1) { $rest = @($all[1..($all.Count - 1)]) }
}
if (@('help', '-h', '--help', '/?') -contains $command) {
    Show-Usage
    exit 0
}
if ($Commands -notcontains $command) {
    Write-Host "kyora.ps1: unknown command '$command'" -ForegroundColor Red
    Show-Usage
    exit 2
}
if ($command -eq 'serve') {
    if ($rest -contains '--no-open') {
        $rest = @($rest | Where-Object { $_ -ne '--no-open' })
    } elseif ($rest -notcontains '--open') {
        $rest = @('--open') + $rest
    }
}

# ------------------------------------------------------------------------------------------- environment
if (-not $env:KYORA_HOME) {
    $env:KYORA_HOME = Join-Path $HOME '.kyora'
}

$python = Find-Python
if (-not $python) {
    Write-Host 'Python 3.10 or newer was not found. Run launchers\install.ps1 first.' -ForegroundColor Red
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
$pyz = Join-Path $root 'kyora.pyz'
if (Test-Path $pyz) {
    $kyoraArgs = @($python.Args) + @($pyz, $command) + $rest
    Write-Host "Kyora: $($python.Exe) $pyz $command (KYORA_HOME=$env:KYORA_HOME)"
} elseif (Test-Path (Join-Path $root 'kyora\__main__.py')) {
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = $root + [IO.Path]::PathSeparator + $env:PYTHONPATH
    } else {
        $env:PYTHONPATH = $root
    }
    $kyoraArgs = @($python.Args) + @('-m', 'kyora', $command) + $rest
    Write-Host "Kyora (source tree): $($python.Exe) -m kyora $command (KYORA_HOME=$env:KYORA_HOME)"
} else {
    Write-Host "Neither $pyz nor a kyora source tree was found next to this script." -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------------------------------------- run
$global:LASTEXITCODE = 0
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $python.Exe @kyoraArgs
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previous
}
exit $code
