# Read-only U7 desktop helper. Python bounds this whole process to 12 seconds.
# No console attachment, title writes, selection, scrolling, or child processes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type @'
using System;
using System.Text;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class DeskWindows {
    public delegate bool Callback(IntPtr h, IntPtr p);
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool EnumWindows(Callback c, IntPtr p);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    public static long[] List() {
        var found = new List<long>();
        if (!EnumWindows((h,p) => {
            var s = new StringBuilder(256);
            GetClassName(h,s,256);
            if(s.ToString()=="CASCADIA_HOSTING_WINDOW_CLASS") found.Add(h.ToInt64());
            return true;
        }, IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return found.ToArray();
    }
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$before = @([DeskWindows]::List())
$condition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::TabItem)
$windows = @(foreach ($handle in $before) {
    $h = [IntPtr]$handle
    $tabs = @()
    $state = 'ok'
    $errorText = $null
    if (-not [DeskWindows]::IsWindow($h)) { $state = 'vanished' }
    else {
        try {
            $root = [Windows.Automation.AutomationElement]::FromHandle($h)
            $elements = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)
            $tabs = @(for ($i = 0; $i -lt $elements.Count; $i++) {
                [ordered]@{ index=$i; name=$elements.Item($i).Current.Name }
            })
            if ($tabs.Count -eq 0) {
                if ([DeskWindows]::IsWindowVisible($h)) { $state = 'visible-zero-tabs' }
                else { $state = 'zero-tabs' }
            }
        } catch {
            $state = 'error'
            $errorText = $_.Exception.Message
        }
        if (-not [DeskWindows]::IsWindow($h)) { $state = 'vanished' }
    }
    [ordered]@{ hwnd=$handle; state=$state; error=$errorText; tabs=$tabs }
})
$after = @([DeskWindows]::List())
[ordered]@{ before=$before; after=$after; windows=$windows } |
    ConvertTo-Json -Depth 8 -Compress
