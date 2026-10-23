<#
.SYNOPSIS
    smoke-emit.ps1 - live-machine smoke test for Snapshot-Desk.ps1.

.DESCRIPTION
    Runs the real emitter against this machine's real ~/.claude/sessions
    registry and asserts on the actual output. This is NOT a substitute for
    the Python-side unit tests (test_procstart.py, test_titles.py,
    test_model_contract.py, ...) that will be built against emit_golden.json
    in a later work unit - it is the "requires live verification" half from
    docs/DESK.md §13.4's second table, scoped to what Snapshot-Desk.ps1
    itself is responsible for.

    IMPORTANT — why this does not literally implement the assertions as
    given in this unit's brief:

    Snapshot-Desk.ps1 does NOT filter on alive / procStart / kind, and does
    NOT strip the title's status-glyph prefix. Per docs/DESK.md §5 ("No
    filtering, no comparison, no hashing" — S1's own scope line in §13.3)
    and §13.2's design note on titles.py ("the strip happens once, in
    Python... putting it in PowerShell would put it in the one component the
    golden-file contract test cannot cover"), every one of those is Python's
    job, not the emitter's. So this script:

      - asserts every RAW returned entry (dead, bg, interactive - all of
        them) has a non-empty sessionId and cwd, since those come straight
        from the registry file and are always present regardless of
        liveness or kind;
      - independently recomputes, from the registry files and Get-Process,
        which entries are alive-and-interactive (the same three checks
        desk snap will apply in Python: liveness, procStart string
        equality, kind == "interactive") and cross-checks the emitted raw
        data against that ground truth;
      - asserts non-empty titleRaw and titleAttachOk == $true only on that
        alive-and-interactive subset, which is the scope in which §3.2's
        measured claim (8/8, 9/9, zero AttachConsole failures) applies. A
        dead or backgrounded entry may legitimately have no attachable
        console, and asserting a non-empty title over the whole raw set
        would be asserting something the design does not claim.

    Exit code 0 on success, non-zero (with a descriptive message) on the
    first failed assertion.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$emitterPath = Join-Path $scriptDir 'Snapshot-Desk.ps1'

function Fail {
    param([string]$Message)
    Write-Host "SMOKE FAIL: $Message" -ForegroundColor Red
    exit 1
}

function Ok {
    param([string]$Message)
    Write-Host "  ok - $Message" -ForegroundColor Green
}

function Warn {
    param([string]$Message)
    Write-Host "  note - $Message" -ForegroundColor Yellow
}

Write-Host "Running $emitterPath -Emit ..."
# Invoked as a genuine child process (matching how Python actually shells out
# to it — §4 "Python shells out to it"), not via in-process `& $emitterPath`.
# Snapshot-Desk.ps1 writes JSON directly to the OS stdout handle (see its own
# comments on why), which bypasses PowerShell's output-pipeline capture when
# dot/call-invoked in the same host process; a real child process gives it a
# real OS pipe to write into, same as the production caller gets.
$rawOutput = & powershell.exe -NoProfile -NonInteractive -File $emitterPath -Emit
$exitCode = $LASTEXITCODE
if ($exitCode -and $exitCode -ne 0) {
    Fail "Snapshot-Desk.ps1 -Emit exited with code $exitCode"
}

# --- assertion 1: output parses as JSON --------------------------------
# L8: reassembling captured child-process output with `Out-String` runs it
# through PowerShell's own formatter (line wrapping / padding), which is not
# a faithful reconstruction of what the emitter wrote. `$rawOutput` is
# already the array of lines PowerShell split stdout into; `-join "`n"`
# reassembles them verbatim instead of reformatting them.
$rawJson = $rawOutput -join "`n"
try {
    $parsed = $rawJson | ConvertFrom-Json -ErrorAction Stop
}
catch {
    Fail "output did not parse as JSON: $($_.Exception.Message)"
}
Ok "output parses as JSON"

if (-not (Get-Member -InputObject $parsed -Name 'sessions' -MemberType NoteProperty)) {
    Fail "top-level object has no 'sessions' property"
}
if (-not (Get-Member -InputObject $parsed -Name 'claudeProcessCount' -MemberType NoteProperty)) {
    Fail "top-level object has no 'claudeProcessCount' property"
}
if (-not (Get-Member -InputObject $parsed -Name 'bootTime' -MemberType NoteProperty)) {
    Fail "top-level object has no 'bootTime' property"
}
Ok "top-level shape has 'sessions', 'claudeProcessCount' and 'bootTime'"

# --- assertion: bootTime is present; null is a legal D1 outcome, --------
# --- otherwise plausible -------------------------------------------------
# §6 defines it as the boot current at capture, epoch ms UTC, always less
# than `capturedAt` (here: less than "now"). Not asserted equal to any
# specific value -- this machine will eventually reboot and the number will
# legitimately change -- only that, when present, it is a real, current-era
# timestamp: not zero, not negative.
#
# `$null` is NOT a smoke failure (review finding L6). D1 settles that a
# `LastBootUpTime` read that fails (WMI service down, permissions, a
# locked-down machine) is guarded on the PowerShell side and emitted as
# `bootTime: $null` rather than killing the whole `-Emit` call -- the same
# H1 failure class this guard exists to avoid. Treating null as a hard
# smoke failure would report the emitter as broken on exactly the machine
# state D1 exists to tolerate. So: null passes with a visible note; when a
# value IS present, the full plausibility checks below still apply.
$epoch2020 = 1577836800000
$nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
if ($null -eq $parsed.bootTime) {
    Warn "bootTime unavailable -- legal per D1, the emitter guarded the CIM read (Win32_OperatingSystem was unreadable on this run)"
}
else {
    $bootTimeValue = [long]$parsed.bootTime
    if ($bootTimeValue -le 0) {
        Fail "bootTime ($bootTimeValue) is not a positive integer"
    }
    if ($bootTimeValue -ge $nowMs) {
        Fail "bootTime ($bootTimeValue) is not less than the current epoch-ms time ($nowMs) -- a boot cannot be in the future"
    }
    if ($bootTimeValue -le $epoch2020) {
        Fail "bootTime ($bootTimeValue) is not greater than epoch-ms for 2020-01-01 ($epoch2020) -- implausibly old"
    }
    Ok "bootTime ($bootTimeValue) is a positive integer, less than now ($nowMs), greater than 2020-01-01 ($epoch2020)"
}

$sessions = @($parsed.sessions)
Write-Host "  ($($sessions.Count) raw registry entries, claudeProcessCount=$($parsed.claudeProcessCount))"

# --- assertion 2: every raw entry has non-empty sessionId and cwd -------
foreach ($s in $sessions) {
    if ([string]::IsNullOrWhiteSpace([string]$s.sessionId)) {
        Fail "entry with pid $($s.pid) has an empty sessionId"
    }
    if ([string]::IsNullOrWhiteSpace([string]$s.cwd)) {
        Fail "entry with pid $($s.pid) (sessionId $($s.sessionId)) has an empty cwd"
    }
}
Ok "every raw entry has a non-empty sessionId and cwd"

# --- assertion 3: raw session count is within ~/.claude/sessions/*.json -
# L8: Snapshot-Desk.ps1 legitimately SKIPS empty/unparseable/pid-less files
# (its own comment at Snapshot-Desk.ps1:141-154) — the emitted count can
# therefore be less than the file count, and that is correct behaviour, not
# a defect. Only an emitted count GREATER than the file count is impossible
# and worth failing on; strict equality was asserting the "modulo
# unreadable/corrupt files" the failure message itself already conceded.
$sessionsDir = Join-Path $HOME '.claude\sessions'
$registryFiles = @(Get-ChildItem -LiteralPath $sessionsDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
if ($sessions.Count -gt $registryFiles.Count) {
    Fail ("raw session count ($($sessions.Count)) exceeds " +
          "~/.claude/sessions/*.json file count ($($registryFiles.Count)) — " +
          "Snapshot-Desk.ps1 cannot emit more entries than there are files")
}
Ok ("raw session count ($($sessions.Count)) is within the " +
    "~/.claude/sessions/*.json file count ($($registryFiles.Count)) — " +
    "modulo unreadable/corrupt/pid-less files, which are legitimately skipped")

# --- independently recompute the alive-and-interactive ground truth -----
# Same three checks Python's `desk snap` will apply (§5 steps 5-7):
# liveness, procStart string-equality against the LIVE value, kind ==
# "interactive". Recomputed here directly from the registry files and
# Get-Process, independently of Snapshot-Desk.ps1's own internals, so this
# is a real cross-check rather than a tautology.
$expectedAliveInteractive = 0
foreach ($file in $registryFiles) {
    try {
        $raw = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
        $entry = $raw | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        continue
    }
    if (-not (Get-Member -InputObject $entry -Name 'pid' -MemberType NoteProperty)) { continue }
    if ($entry.kind -ne 'interactive') { continue }

    # H1: guard entryPid <= 0 before Get-Process (pid 0 is the Idle process,
    # not "no process") and wrap the .StartTime read — Get-Process succeeds
    # for a protected/system process but its .StartTime can be $null, and
    # .ToFileTimeUtc() on $null is a terminating error. Same defect and same
    # fix as Snapshot-Desk.ps1's own procStartLive read: on failure, this
    # entry simply does not match and is excluded from the ground truth,
    # mirroring Python's string-equality guard rejecting it.
    $entryPid = [int]$entry.pid
    if ($entryPid -le 0) { continue }

    $proc = Get-Process -Id $entryPid -ErrorAction SilentlyContinue
    if (-not $proc) { continue }

    try {
        $liveProcStart = $proc.StartTime.ToFileTimeUtc().ToString()
    }
    catch {
        continue
    }
    if ($liveProcStart -cne [string]$entry.procStart) { continue }

    $expectedAliveInteractive++
}

$emittedAliveInteractive = @(
    $sessions | Where-Object {
        $_.alive -eq $true -and
        $_.kind -eq 'interactive' -and
        ([string]$_.procStart) -ceq ([string]$_.procStartLive)
    }
)

if ($emittedAliveInteractive.Count -ne $expectedAliveInteractive) {
    Fail ("alive-and-interactive count derived from the emitted data " +
          "($($emittedAliveInteractive.Count)) does not match the independently " +
          "recomputed ground truth ($expectedAliveInteractive)")
}
Ok "alive-and-interactive count matches ground truth ($expectedAliveInteractive of $($registryFiles.Count) registry entries)"

# --- assertion 4: no empty title, and a successful attach, among the ----
#     alive-and-interactive subset (§3.2's measured claim applies here)
foreach ($s in $emittedAliveInteractive) {
    if ($s.titleAttachOk -ne $true) {
        Fail "alive interactive session $($s.sessionId) (pid $($s.pid)) has titleAttachOk == false"
    }
    if ([string]::IsNullOrEmpty([string]$s.titleRaw)) {
        Fail "alive interactive session $($s.sessionId) (pid $($s.pid)) has an empty titleRaw"
    }
}
Ok "every alive-and-interactive session has titleAttachOk == true and a non-empty titleRaw"

Write-Host ""
Write-Host "SMOKE PASS: $($sessions.Count) raw entries, $($emittedAliveInteractive.Count) alive-and-interactive, claudeProcessCount=$($parsed.claudeProcessCount)" -ForegroundColor Green
exit 0
