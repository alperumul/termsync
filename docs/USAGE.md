# Using Termsync

Use the [README setup](../README.md#quick-start) once, then run `termsync` in an
interactive Windows terminal to choose sessions to restore. Before adding the
checkout to PATH, use `.\termsync.cmd` from its directory. The equivalent Python
entry point requires an explicit command, such as `python -m termsync restore`.

## Choosing a saved desk

Termsync selects a larger recent desk so a partly closed terminal does not
automatically replace your recovery view. The header explains when this differs
from the newest capture. Press `n` to switch between them, or start with
`termsync restore --newest`. Switching preserves your surviving selections.

Filtering matches session titles and directories. While typing a filter, letters
such as `a`, `n`, and `q` are text; press Enter to finish typing before using them
as commands. Filtering hides rows without deselecting them. `a` clears the whole
selection. Armed and archived rows start unchecked.

`termsync restore --dry-run` prints full session IDs and availability without
opening tabs or entering the keyboard loop. With a saved desk it reads current
provider and desktop state, so it can still report reader errors.

## Restoring safely

Normal restore opens fresh windows and leaves each tab waiting for Enter.
`termsync restore --run` resumes the selected sessions immediately after the
picker. Captured Claude groups keep their order; unknown placement, Codex, and
other rows without a usable captured position go to the final window.

The launcher rechecks conversation files, Codex resume metadata, and working
directories before opening tabs. Sessions already live when the picker is built
are unavailable. A session started elsewhere while the picker stays open is
not continuously repolled; quit and reopen the picker to refresh it.

If a provider's current-session read fails, Termsync shows the error and blocks
restore for that provider until you reopen the picker after resolving it.
Sessions from a provider whose read succeeded remain usable.

Checkout and project paths containing semicolons (`;`) cannot be restored:
Windows Terminal treats them as command separators. Use paths without
semicolons. After moving the checkout, recreate its venv and update PATH and
any installed capture task. After moving a project, open its provider session
at the new location and capture it again; old snapshots retain the old path.

Accepted launches are retained even when later verification fails. A nonzero
exit may mean some tabs opened and others were skipped or not attempted.
Inspect the printed result and open tabs before retrying. Termsync does not
automatically retry uncertain launches or close them.

For `--run`, missing exact provider identities at verification time produce a
failure; that observation does not prove they can never start later. Armed
restore reports pending entries informationally because the periodic capture
may already have reconciled a resumed tab. Window readiness alone does not
prove conversation identity.

## Captures and local data

`termsync snap` captures immediately. `termsync install` explicitly registers
the ten-minute windowless capture task described in the README; ordinary usage
does not require reinstalling it. Power-event triggers are best effort.

The default store is `%USERPROFILE%\.termsync`. It contains captured layouts,
capture status, pending armed entries, and restore action records. The provider
retains the conversation content. Store files contain session IDs, titles, and
directories: keep them private when sharing a checkout or diagnostic report.
An unchanged inventory refreshes the latest capture instead of allocating a
new version. Retention protects at least 30 days and the selected recovery
records; restore action records themselves are never restore targets.

For a separate store:

```powershell
termsync snap --store C:\TermsyncData\test-desk
termsync restore --dry-run --store C:\TermsyncData\test-desk
termsync restore --store C:\TermsyncData\test-desk
```

The first command reads your current supported sessions and writes their
inventory to that directory. It is not a synthetic test. An isolated store does
not change where Claude or Codex keeps its conversations.

## When something looks wrong

### PowerShell script permissions

The restore launcher runs `snapshot\arm.ps1` through Windows PowerShell. That
script must be allowed by your execution policy. A blocked script can leave a
tab open without reaching the Enter prompt, even when capture works.

Inspect the policies from PowerShell:

```powershell
Get-ExecutionPolicy -List
```

`Restricted` blocks scripts. `RemoteSigned` allows local scripts, but Windows
may mark files downloaded in an archive as coming from the internet. Review
the source before allowing a trusted script to run. On a managed machine,
follow your organization's policy. Microsoft describes the available settings
and downloaded-file handling in its
[execution-policy guide](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1).
Termsync does not change those settings for you.

### Other symptoms

| Symptom | Next step |
| --- | --- |
| `venv python not found` | Repeat the README setup in the same checkout |
| Picker refuses non-interactive input | Open an interactive terminal, or use `restore --dry-run` |
| No desk captured yet | Run `snap`, or check whether you selected the intended store |
| Old snapshot or repeated capture failures | Inspect the header and `status.json`; run `snap` to see the current error |
| Missing conversation or directory | Restore the provider files/directory, then reopen the picker |
| Unknown window placement | Restore into the final window; ambiguous placement is intentionally not guessed |
| Unverified or partial restore | Inspect accepted tabs and the printed result before another attempt |

[Development and diagnostic checks](DEVELOPMENT.md) distinguish offline tests
from checks that read the installed task or launch disposable provider sessions.
