<# Read-only Windows observations for Codex capture. No title writes or launches.
   Processes returns raw argv, exact start FILETIME and console attachment.
   Locks reads a JSON list of paths on stdin and returns each lock's holders.
   Caller enforces a process timeout; never call RmShutdown or RmRestart. #>
[CmdletBinding()]
param([ValidateSet('Processes', 'Locks')][string]$Mode = 'Processes')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class CodexObservation {
    [DllImport("kernel32.dll")] public static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool AttachConsole(uint pid);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CommandLineToArgvW(string cmd, out int argc);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr ptr);
    public static string[] Argv(string command) {
        int count; var ptr = CommandLineToArgvW(command, out count);
        if (ptr == IntPtr.Zero) throw new Exception("CommandLineToArgvW failed");
        try {
            var args = new string[count];
            for (int i=0; i<count; i++)
                args[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(ptr, i*IntPtr.Size));
            return args;
        } finally { LocalFree(ptr); }
    }
    [StructLayout(LayoutKind.Sequential)] public struct UniqueProcess {
        public uint pid;
        public System.Runtime.InteropServices.ComTypes.FILETIME start;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct ProcessInfo {
        public UniqueProcess process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string app;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=64)] public string service;
        public uint type, status, session;
        [MarshalAs(UnmanagedType.Bool)] public bool restartable;
    }
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)]
    static extern int RmStartSession(out uint handle, uint flags, StringBuilder key);
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)]
    static extern int RmRegisterResources(uint handle, uint files, string[] paths,
        uint apps, UniqueProcess[] processes, uint services, string[] names);
    [DllImport("rstrtmgr.dll")]
    static extern int RmGetList(uint handle, out uint needed, ref uint count,
        [In, Out] ProcessInfo[] processes, ref uint reasons);
    [DllImport("rstrtmgr.dll")] static extern int RmEndSession(uint handle);
    public static ProcessInfo[] Holders(string path) {
        uint handle; int code = RmStartSession(out handle, 0, new StringBuilder(33));
        if (code != 0) throw new Exception("RmStartSession: " + code);
        try {
            code = RmRegisterResources(handle, 1, new string[]{path}, 0, null, 0, null);
            if (code != 0) throw new Exception("RmRegisterResources: " + code);
            uint needed=0, count=0, reasons=0;
            code = RmGetList(handle, out needed, ref count, null, ref reasons);
            if (code == 0) return new ProcessInfo[0];
            for (int attempt=0; code==234 && attempt<3; attempt++) {
                if (needed > 1024) throw new Exception("Too many lock holders");
                var rows = new ProcessInfo[needed]; count=needed;
                code = RmGetList(handle, out needed, ref count, rows, ref reasons);
                if (code==0) { Array.Resize(ref rows, (int)count); return rows; }
            }
            throw new Exception("RmGetList: " + code);
        } finally { RmEndSession(handle); }
    }
    public static string Start(UniqueProcess process) {
        return (((ulong)(uint)process.start.dwHighDateTime << 32)
            | (uint)process.start.dwLowDateTime).ToString();
    }
}
'@
$records = @()
if ($Mode -eq 'Processes') {
    foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'codex.exe'")) {
        try {
            $start = (Get-Process -Id $process.ProcessId -ErrorAction Stop).StartTime.ToFileTimeUtc().ToString()
            $argv = @()
            if ($process.CommandLine) { $argv = @([CodexObservation]::Argv($process.CommandLine)) }
            [void][CodexObservation]::FreeConsole()
            $attached = [CodexObservation]::AttachConsole([uint32]$process.ProcessId)
            $records += [pscustomobject]@{
                pid = [int]$process.ProcessId; procStart = $start
                argv = $argv; console = $attached
            }
        } catch {
            # A process may exit during enumeration; report uncertainty to Python.
            $records += [pscustomobject]@{ pid = [int]$process.ProcessId; error = $_.Exception.Message }
        } finally { [void][CodexObservation]::FreeConsole() }
    }
} else {
    # PS 5.1 keeps ConvertFrom-Json's root array as one pipeline object.
    # Wrapping that pipeline in @() nests it; foreach would pass all paths
    # joined with spaces to Restart Manager. Assign the root array directly.
    $paths = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
    foreach ($path in $paths) {
        try {
            $holders = @([CodexObservation]::Holders([string]$path) | ForEach-Object {
                [pscustomobject]@{ pid = [int]$_.process.pid; procStart = [CodexObservation]::Start($_.process) }
            })
            $records += [pscustomobject]@{ path = [string]$path; holders = $holders }
        } catch {
            $records += [pscustomobject]@{ path = [string]$path; error = $_.Exception.Message; holders = @() }
        }
    }
}
$json = ConvertTo-Json -InputObject @($records) -Depth 8
$writer = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), (New-Object System.Text.UTF8Encoding($false)))
try { $writer.Write($json); $writer.Flush() } finally { $writer.Dispose() }
