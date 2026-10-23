# Confirm only a current run-owned multi-tab close dialog. No foreground input.
param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$owned = Get-Content -Raw -Encoding UTF8 -LiteralPath $Request | ConvertFrom-Json
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class LiveCloseWindow {
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$handle = [IntPtr]$owned.hwnd
if (-not [LiveCloseWindow]::IsWindow($handle)) {
    Write-Output '{"alreadyAbsent":true}'
    exit 0
}
[uint32]$windowPid = 0
[void][LiveCloseWindow]::GetWindowThreadProcessId($handle, [ref]$windowPid)
$process = Get-Process -Id $windowPid
if (($windowPid -ne $owned.stamp.pid) -or
    ($process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -ne $owned.stamp.start)) {
    throw 'Refusing changed window process identity.'
}
$root = [Windows.Automation.AutomationElement]::FromHandle($handle)
$tabCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::TabItem)
$tabs = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $tabCondition)
if ($tabs.Count -ne $owned.titles.Count) { throw 'Refusing changed tab count.' }
for ($i = 0; $i -lt $tabs.Count; $i++) {
    if ($tabs.Item($i).Current.Name -cne $owned.titles[$i]) { throw 'Refusing changed tab title/order.' }
}
$dialogCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::NameProperty, 'Do you want to close all tabs?')
$matches = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $dialogCondition)
$dialogs = @($matches | Where-Object { $_.Current.ControlType -eq [Windows.Automation.ControlType]::Window })
if ($dialogs.Count -eq 0) { Write-Output '{"dialogPresent":false}'; exit 0 }
if ($dialogs.Count -ne 1) { throw 'Ambiguous close confirmation.' }
$buttonCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::NameProperty, 'Close all')
$buttons = $dialogs[0].FindAll([Windows.Automation.TreeScope]::Descendants, $buttonCondition)
if (($buttons.Count -ne 1) -or
    ($buttons.Item(0).Current.ControlType -ne [Windows.Automation.ControlType]::Button)) {
    throw 'Missing exact Close all button.'
}
$pattern = $buttons.Item(0).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
$pattern.Invoke()
Write-Output '{"dialogPresent":true,"invoked":true}'
