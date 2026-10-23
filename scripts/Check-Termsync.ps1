# Repeatable, non-launching verification. Never installs a task or restores sessions.
param([string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$runDirectory = Join-Path $projectRoot ('scratch/verification/' + $runId)
$tempRoot = [IO.Path]::GetFullPath($env:TEMP)
$testDirectory = Join-Path $tempRoot ('termsync-verification-' + $runId)
[void](New-Item -ItemType Directory -Path $runDirectory)

if (-not $Python) {
    $Python = Join-Path $env:LOCALAPPDATA 'Python/pythoncore-3.14-64/python.exe'
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Python with pytest was not found. Pass -Python with its full path.'
}

$summary = [ordered]@{
    scope = 'Automated isolated restore regressions and read-only installed-task health'
    liveRestorePerformed = $false
    startedAt = [DateTime]::UtcNow.ToString('o')
    reportDirectory = $runDirectory
    taskRead = $false
    captureInterval = $null
    windowlessAction = $false
    snapshotOutcome = $null
    snapshotAgeMinutes = $null
    snapshotDurationMs = $null
    testsExitCode = $null
    result = 'HOLD'
    errors = @()
}

try {
    $taskLines = & schtasks.exe /Query /TN termsync-snap /XML ONE
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the installed termsync-snap task.' }
    [xml]$task = $taskLines -join [Environment]::NewLine
    $ns = New-Object System.Xml.XmlNamespaceManager($task.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $summary.taskRead = $true
    $summary.captureInterval = $task.SelectSingleNode('//t:TimeTrigger/t:Repetition/t:Interval', $ns).InnerText
    $command = $task.SelectSingleNode('//t:Actions/t:Exec/t:Command', $ns).InnerText.Trim('"')
    $arguments = $task.SelectSingleNode('//t:Actions/t:Exec/t:Arguments', $ns).InnerText
    $expectedPython = Join-Path $projectRoot '.venv/Scripts/pythonw.exe'
    $summary.windowlessAction = ([IO.Path]::GetFullPath($command) -eq [IO.Path]::GetFullPath($expectedPython)) -and ($arguments -eq '-m termsync snap')
} catch {
    $summary.errors += $_.Exception.Message
}

try {
    $status = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $env:USERPROFILE '.termsync/status.json') | ConvertFrom-Json
    $summary.snapshotOutcome = $status.outcome
    $summary.snapshotDurationMs = $status.durationMs
    if ($null -eq $status.finishedAt) { throw 'Latest status has no completion timestamp.' }
    $finished = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$status.finishedAt)
    $summary.snapshotAgeMinutes = [Math]::Round(([DateTimeOffset]::UtcNow - $finished).TotalMinutes, 2)
} catch {
    $summary.errors += $_.Exception.Message
}

$oldPythonPath = $env:PYTHONPATH
$oldBytecode = $env:PYTHONDONTWRITEBYTECODE
Push-Location $projectRoot
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $testArgs = @('-m', 'pytest', '-o', 'addopts=', '-q', '-p', 'no:cacheprovider',
        '--basetemp', $testDirectory,
        'tests/test_restore_windows.py', 'tests/test_restore_cmdline.py',
        'tests/test_restore_codex.py', 'tests/test_picker.py',
        'tests/test_dry_run.py', 'tests/test_windows_replay.py',
        'tests/test_u10_windows_review.py')
    & $Python @testArgs 2>&1 | Tee-Object -FilePath (Join-Path $runDirectory 'tests.txt')
    $summary.testsExitCode = $LASTEXITCODE
} catch {
    $summary.errors += $_.Exception.Message
} finally {
    $env:PYTHONPATH = $oldPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $oldBytecode
    Pop-Location
    try {
        if (Test-Path -LiteralPath $testDirectory) {
            $resolved = (Resolve-Path -LiteralPath $testDirectory).Path
            if (($resolved -ne [IO.Path]::GetFullPath($testDirectory)) -or
                ((Split-Path $resolved -Parent).TrimEnd('\') -ne $tempRoot.TrimEnd('\'))) {
                throw 'Refusing test cleanup outside the exact per-run temporary directory.'
            }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    } catch {
        $summary.errors += ('Temporary test cleanup: ' + $_.Exception.Message)
    }
}

$healthy = $summary.taskRead -and ($summary.captureInterval -eq 'PT10M') -and
    $summary.windowlessAction -and ($summary.snapshotOutcome -eq 'ok') -and
    ($null -ne $summary.snapshotAgeMinutes) -and ($summary.snapshotAgeMinutes -ge 0) -and
    ($summary.snapshotAgeMinutes -le 25) -and ($summary.errors.Count -eq 0)
if (($summary.testsExitCode -eq 0) -and $healthy) { $summary.result = 'PASS' }
$summary.finishedAt = [DateTime]::UtcNow.ToString('o')
$summary | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $runDirectory 'summary.json')
Write-Host ($summary.result + ': isolated restore checks and current capture health.')
Write-Host 'No Terminal/provider was launched. This is not a live restore acceptance result.'
Write-Host ('Evidence: ' + $runDirectory)
if ($summary.result -eq 'PASS') { exit 0 } else { exit 1 }
