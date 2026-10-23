<#
.SYNOPSIS
    Read Claude session registry and desktop process facts as UTF-8 JSON.
.DESCRIPTION
    Snapshot-Desk.ps1 -Emit reads ~/.claude/sessions/*.json, process liveness,
    exact live process-start FILETIME strings, raw console titles, the Claude
    process count, and boot time. It writes no files and spawns no children.
    Python owns identity filtering, title normalization, and capture policy.
.NOTES
    titleAttachOk distinguishes an unreadable title from a registry fallback.
    claudeProcessCount is independent of the registry, allowing detection of
    running processes with no readable entries. bootTime is epoch milliseconds
    UTC, or null when unavailable; a failed boot read must not lose the desk.
#>

[CmdletBinding()]
param(
    # Retained call shape; this read-only helper has a single mode.
    [switch]$Emit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Win32 interop. Read-only surface: FreeConsole, AttachConsole,
# GetConsoleTitleW. SetConsoleTitleW is deliberately not declared here.
# ---------------------------------------------------------------------------
if (-not ('Desk.Interop.NativeMethods' -as [type])) {
    Add-Type -Namespace Desk.Interop -Name NativeMethods -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool FreeConsole();

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool AttachConsole(uint dwProcessId);

[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern uint GetConsoleTitleW(System.Text.StringBuilder lpConsoleTitle, uint nSize);
'@
}

# Console title buffer. GetConsoleTitleW's documented max is 65535 chars;
# every observed Claude Code title is well under a few hundred, so 4096 is
# generously sized without paying for the theoretical maximum on every call.
$script:TitleBufferSize = 4096

function Get-RawConsoleTitle {
    <#
        Read-only. FreeConsole() detaches from whatever console this process
        currently owns (including, on the very first call, this script's own
        host console — §3.2's mechanical requirement). AttachConsole(pid)
        attaches to the target session's console. GetConsoleTitleW reads its
        title verbatim, unstripped. The `finally` block frees the attached
        console again so the next iteration's FreeConsole/AttachConsole pair
        starts clean and so this process does not exit still attached to
        someone else's console.

        Never calls SetConsoleTitleW. Never spawns a child process while
        detached (§ hard constraints) - this function does no process
        creation of any kind.
    #>
    param(
        [Parameter(Mandatory)]
        [int]$ProcessId
    )

    [void][Desk.Interop.NativeMethods]::FreeConsole()

    try {
        $attached = [Desk.Interop.NativeMethods]::AttachConsole([uint32]$ProcessId)
        if (-not $attached) {
            return [pscustomobject]@{
                titleRaw      = $null
                titleAttachOk = $false
            }
        }

        $sb = New-Object System.Text.StringBuilder($script:TitleBufferSize)
        [void][Desk.Interop.NativeMethods]::GetConsoleTitleW($sb, [uint32]$script:TitleBufferSize)
        return [pscustomobject]@{
            titleRaw      = $sb.ToString()
            titleAttachOk = $true
        }
    }
    finally {
        # Always detach after an attempted attach, including the failure path.
        # The next title read must start from a known detached state.
        [void][Desk.Interop.NativeMethods]::FreeConsole()
    }
}

# ---------------------------------------------------------------------------
# 1. Read every ~/.claude/sessions/*.json verbatim (§5 step 1).
# ---------------------------------------------------------------------------
$sessionsDir = Join-Path $HOME '.claude\sessions'
$entries = @()

$readerErrors = @()
if (Test-Path -LiteralPath $sessionsDir -PathType Container) {
    $files = @()
    try {
        $files = @(Get-ChildItem -LiteralPath $sessionsDir -Filter '*.json' -File -ErrorAction Stop)
    } catch {
        $readerErrors += "Claude registry enumeration: $($_.Exception.Message)"
    }

    foreach ($file in $files) {
        # A registry file that fails to parse is not a "readable registry
        # entry" (§8's primary error rule is phrased exactly that way) — skip
        # it rather than emit a broken record. This does not fire when the
        # file is a legitimate session entry; it only guards a torn or
        # corrupt write on Claude Code's side. §5 step 1 says "read verbatim"
        # and gives no instruction for an unreadable file, so this is a
        # necessary but not explicitly specified choice.
        #
        # M9: skipping these entries defines "readable" for health.py's
        # `is_registry_error` rule and its `readable_entry_count` parameter — a
        # file that skips here never appears in the emitted `sessions` array,
        # so `len(emit["sessions"])` on the Python side already excludes it.
        # Nothing downstream needs to re-derive "readable"; this is its one
        # source. U7 also reports skipped entries: a partial registry cannot
        # prove global title uniqueness, but successful rows remain usable.
        try {
            $raw = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
            if ([string]::IsNullOrWhiteSpace($raw)) { throw 'Empty registry entry' }
            $entry = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($null -eq $entry -or $entry -isnot [pscustomobject]) {
                throw 'Registry entry is not an object'
            }
            if (-not (Get-Member -InputObject $entry -Name 'pid' -MemberType NoteProperty)) {
                throw 'Registry entry has no pid'
            }
            $entryPid = [int]$entry.pid
        }
        catch {
            $readerErrors += "Claude registry $($file.Name): $($_.Exception.Message)"
            continue
        }

        # -----------------------------------------------------------------
        # 2. Liveness + procStartLive (§5 step 2). No comparison against the
        #    registry's own procStart happens here — that is Python's job
        #    (§5 step 6, the mandatory string-equality pid-reuse guard).
        #
        #    H1: a torn registry write can produce "pid": null, which
        #    [int]$entry.pid coerces to 0, and Get-Process -Id 0 returns the
        #    Idle process rather than $null — so guard entryPid <= 0 before
        #    calling Get-Process at all, treating it the same as "no process".
        # -----------------------------------------------------------------
        if ($entryPid -le 0) {
            $proc = $null
        }
        else {
            $proc = Get-Process -Id $entryPid -ErrorAction SilentlyContinue
        }

        if ($proc) {
            $alive = $true
            # MANDATED source (§5's procStart rule, rule 1): Get-Process's
            # .StartTime.ToFileTimeUtc(), never Get-CimInstance Win32_Process
            # (7-tick disagreement measured, forbidden). .ToString() keeps
            # this an 18-character decimal string, never a numeric type, so
            # Python's comparison stays string-ordinal with no float in the
            # path (rules 2 and 3).
            #
            # H1: Get-Process -Id N succeeds for protected/system/elevated
            # processes but returns an object whose .StartTime is $null;
            # calling .ToFileTimeUtc() on $null is a TERMINATING error that
            # $ErrorActionPreference cannot downgrade, and it killed the
            # whole script with empty stdout. On failure, leave
            # $procStartLive = $null and let Python's existing string-
            # equality guard (§5 step 6) reject the entry on its own terms —
            # no new filter or decision is added here.
            $procStartLive = $null
            try {
                $procStartLive = $proc.StartTime.ToFileTimeUtc().ToString()
            }
            catch {
                $procStartLive = $null
            }
        }
        else {
            $alive = $false
            $procStartLive = $null
        }

        # -----------------------------------------------------------------
        # 3. Raw console title (§5 step 3). Read-only; nothing is written.
        # -----------------------------------------------------------------
        $titleInfo = Get-RawConsoleTitle -ProcessId $entryPid

        $entry | Add-Member -NotePropertyName 'alive'         -NotePropertyValue $alive         -Force
        $entry | Add-Member -NotePropertyName 'procStartLive' -NotePropertyValue $procStartLive  -Force
        $entry | Add-Member -NotePropertyName 'titleRaw'      -NotePropertyValue $titleInfo.titleRaw      -Force
        $entry | Add-Member -NotePropertyName 'titleAttachOk' -NotePropertyValue $titleInfo.titleAttachOk -Force

        $entries += $entry
    }
}

# ---------------------------------------------------------------------------
# Reattach to the originating console after all title reads. MaxValue is
# ATTACH_PARENT_PROCESS. Failure is harmless if the process has no parent console.
# Production reads this script through a pipe; this is not an interactive-UI claim.
[void][Desk.Interop.NativeMethods]::AttachConsole([uint32]::MaxValue)

# claude.exe process count - see .NOTES above. Read live, not derived from
# the registry entries (a claude.exe with zero registry entries is exactly
# the condition health.py's primary error rule exists to catch).
$claudeProcessCount = @(Get-Process -Name 'claude' -ErrorAction SilentlyContinue).Count

# ---------------------------------------------------------------------------
# Convert the local boot timestamp to UTC before computing epoch milliseconds.
# Preserve millisecond precision; WMI failure leaves boot identity unknown.
$bootTime = $null
try {
    $lastBootUpTime = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime
    $bootTime = [long]([DateTimeOffset]::new($lastBootUpTime.ToUniversalTime(), [TimeSpan]::Zero).ToUnixTimeMilliseconds())
}
catch {
    $bootTime = $null
}

# ---------------------------------------------------------------------------
# 4. Emit UTF-8 JSON to stdout. `sessions` is force-wrapped with @() so a
#    single surviving entry still serialises as a one-element JSON array,
#    never an unwrapped bare object (the documented ConvertTo-Json gotcha).
#    Writing is done via the raw stdout stream rather than [Console]::Out /
#    [Console]::OutputEncoding: this process has already called FreeConsole()
#    above (possibly repeatedly), and setting [Console]::OutputEncoding after
#    that point throws "The handle is invalid" on this machine (verified).
#    Opening the standard output stream directly and writing UTF-8 bytes
#    ourselves sidesteps the console object entirely and works whether
#    stdout is a pipe (the normal desk snap / desk restore invocation) or an
#    interactive console (§11 open item 8).
# ---------------------------------------------------------------------------
$result = [pscustomobject]@{
    sessions           = @($entries)
    claudeProcessCount = $claudeProcessCount
    bootTime           = $bootTime
    claudeError        = $(if ($readerErrors.Count) { $readerErrors -join '; ' } else { $null })
}

$json = ConvertTo-Json -InputObject $result -Depth 10

$stdout = [Console]::OpenStandardOutput()
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$writer = New-Object System.IO.StreamWriter($stdout, $utf8NoBom)
try {
    $writer.Write($json)
    $writer.Flush()
}
finally {
    $writer.Dispose()
}
