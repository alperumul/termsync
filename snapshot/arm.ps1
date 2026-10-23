<#
.SYNOPSIS
    arm.ps1 -- the tab-side half of work unit S6 (§7 step 7, D14, D15, D23).

.DESCRIPTION
    Runs INSIDE the tab `wt.exe new-tab` opens, hosted under
    `powershell.exe -NoExit -NoProfile` so the tab survives Claude exiting
    (§7 step 7's own reasoning: a tab whose command IS `claude --resume`
    with no shell beneath it closes the instant Claude does).

    Prints the captured title, prompt count and age -- fed VERBATIM from
    Python's `picker.prompt_and_age_text` via -Prompts/-Age (D15: phrasing
    has exactly one owner, and it is not this script; no "prompts"/"ago"
    is appended here). Then waits for Enter UNLESS -Run was passed, then
    resumes the session.

    Never `--fork-session`: §7 step 2's already-live union relies on
    `claude --resume <id>` reusing the session id, not minting a new one.

    **-TitleB64, not -Title (D23/F3, S6 review).** The title arrives
    base64-encoded. A title that happened to be a bare `-Word` colliding
    with one of this param block's own names (`-Run`, `-Title`,
    `-SessionId`, `-Age`, `-Prompts`, or an unambiguous prefix of one, or a
    bare `-`/`--`) broke PowerShell's own param binding when passed as a
    plain string -- measured directly: `arm.ps1 : Missing an argument for
    parameter 'Title'`, the tab dying silently under -NoExit with a red
    error while `pending.json` had already recorded success. Base64's
    alphabet (A-Za-z0-9+/=) never produces a leading `-`, so no title can
    ever be misread as a parameter name -- unambiguous for every input, no
    sanitising heuristics, and the decoded title is byte-identical to the
    captured one.

.NOTES
    Zero semicolons in this file's OWN content are not the point (§3.10 is
    about the `wt` COMMAND LINE that invokes this script, not this script's
    body) -- `restore.py.build_wt_argv` is what keeps the wt.exe argv itself
    semicolon-free.
#>
param(
    [string]$SessionId,
    [string]$TitleB64,
    [string]$Prompts,
    [string]$Age,
    [ValidateSet('claude', 'codex')]
    [string]$Agent = 'claude',
    [string]$CwdB64,
    [string]$ResumeArgsB64,
    [switch]$Archived,
    [switch]$Run
)

# D23/F3: decode the base64 title. See this file's own .DESCRIPTION above
# and restore.py's build_wt_argv docstring for why the title never crosses
# this boundary as a plain string.
$Title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($TitleB64))

# D15: -Prompts/-Age arrive already phrased ("5 prompts", "3 minutes ago"),
# never appended to here. restore.py's build_wt_argv omits -Prompts/-Age
# from the argv entirely (rather than passing an empty string) when their
# phrasing is unavailable, so $Prompts/$Age can legitimately be $null here
# -- PowerShell's own default for an omitted [string] parameter. Handled
# without assuming either is present.
$parts = New-Object System.Collections.Generic.List[string]
if ($Prompts) { $parts.Add($Prompts) }
if ($Age) { $parts.Add($Age) }

if ($parts.Count -gt 0) {
    Write-Host ("$Title - " + ($parts -join ', '))
} else {
    Write-Host $Title
}

# Decode data as arguments, never as executable PowerShell text.
$ErrorActionPreference = 'Stop'
$ResumeArgs = @()
if ($Agent -eq 'codex') {
    $ParsedId = [Guid]::Empty
    if (-not [Guid]::TryParseExact($SessionId, 'D', [ref]$ParsedId)) {
        throw 'Invalid Codex session id'
    }
    $Cwd = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($CwdB64))
    if (-not $Cwd -or -not (Test-Path -LiteralPath $Cwd -PathType Container)) {
        throw 'Codex working directory is unavailable'
    }
    $ResumeArgs = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($ResumeArgsB64)))
    if ($ResumeArgs.Count % 2 -ne 0) { throw 'Invalid Codex resume flags' }
    $Seen = @{}
    for ($i = 0; $i -lt $ResumeArgs.Count; $i += 2) {
        $Flag = $ResumeArgs[$i]
        $Value = $ResumeArgs[$i + 1]
        if ($Flag -notin @('-m', '-s', '-a') -or $Seen.ContainsKey($Flag)) {
            throw 'Invalid Codex resume flags'
        }
        if ($Value -isnot [string] -or -not $Value -or $Value.StartsWith('-') -or
            $Value.IndexOfAny([char[]]@(0, 10, 13)) -ge 0) {
            throw 'Invalid Codex resume flag value'
        }
        $Seen[$Flag] = $true
    }
    if ($Archived) { Write-Host 'Archived Codex session: Enter will unarchive and resume.' }
}

if (-not $Run) {
    Read-Host 'Enter to resume'
}

# Never --fork-session (§7 step 2's already-live union depends on --resume
# reusing $SessionId, not minting a new one).
if ($Agent -eq 'codex') {
    if ($Archived) {
        # Do not resume if unarchiving failed. Both actions are behind the gate.
        $global:LASTEXITCODE = 0
        codex unarchive $SessionId
        if ($LASTEXITCODE -ne 0) { throw "Codex unarchive failed (exit $LASTEXITCODE)" }
    }
    codex resume $SessionId -C $Cwd @ResumeArgs
} else {
    claude --resume $SessionId
}
