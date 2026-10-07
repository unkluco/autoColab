param(
    [string]$ConfigFile = '',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'

# One non-inheritable job handle belongs to this supervisor. Windows closes it
# even if the console is closed or this process is forcibly terminated.
if (-not ('AutoColabConsoleJobNative' -as [type])) {
    Add-Type @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class AutoColabConsoleJobNative
{
    public const uint CREATE_SUSPENDED = 0x00000004;
    public const uint STARTF_USESTDHANDLES = 0x00000100;
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    public const uint SYNCHRONIZE = 0x00100000;
    public const uint WAIT_TIMEOUT = 0x00000102;
    public const uint WAIT_FAILED = 0xFFFFFFFF;

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    {
        public long TotalUserTime, TotalKernelTime, ThisPeriodTotalUserTime, ThisPeriodTotalKernelTime;
        public uint TotalPageFaultCount, TotalProcesses, ActiveProcesses, TotalTerminatedProcesses;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO
    {
        public uint cb;
        public string lpReserved, lpDesktop, lpTitle;
        public uint dwX, dwY, dwXSize, dwYSize;
        public uint dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public ushort wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public uint dwProcessId, dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION
    {
        public IntPtr Reserved1, PebBaseAddress, Reserved2a, Reserved2b;
        public UIntPtr UniqueProcessId, InheritedFromUniqueProcessId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES
    {
        public uint nLength;
        public IntPtr lpSecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)] public bool bInheritHandle;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(IntPtr job, int type, IntPtr info, uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool QueryInformationJobObject(IntPtr job, int type,
        out JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info, uint size, IntPtr returnedSize);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CreateProcess(string application, StringBuilder command,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        uint flags, IntPtr environment, string directory, ref STARTUPINFO startup,
        out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForMultipleObjects(uint count, IntPtr[] handles,
        bool waitAll, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(uint access, bool inherit, uint id);
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetStdHandle(int kind);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr source,
        IntPtr targetProcess, out IntPtr target, uint access, bool inherit, uint options);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(string name, uint access, uint share,
        ref SECURITY_ATTRIBUTES attributes, uint disposition, uint flags, IntPtr template);
    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr process, int type,
        out PROCESS_BASIC_INFORMATION info, int size, out int returnedSize);

    public static uint ParentProcessId()
    {
        PROCESS_BASIC_INFORMATION info;
        int returned;
        int status = NtQueryInformationProcess(GetCurrentProcess(), 0, out info,
            Marshal.SizeOf(typeof(PROCESS_BASIC_INFORMATION)), out returned);
        if (status != 0)
            throw new InvalidOperationException("Cannot identify the launcher process (NTSTATUS=" + status + ").");
        return checked((uint)info.InheritedFromUniqueProcessId.ToUInt64());
    }

    public static IntPtr InheritableStandardHandle(int kind)
    {
        IntPtr source = GetStdHandle(kind);
        if (source != IntPtr.Zero && source != new IntPtr(-1))
        {
            IntPtr copy;
            if (!DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(),
                    out copy, 0, true, 2))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inherit console output/input.");
            return copy;
        }
        // A hidden test launcher may have no stdin. Give Python a valid handle.
        SECURITY_ATTRIBUTES attributes = new SECURITY_ATTRIBUTES();
        attributes.nLength = (uint)Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        attributes.bInheritHandle = true;
        IntPtr result = CreateFile("NUL", kind == -10 ? 0x80000000u : 0x40000000u,
            3, ref attributes, 3, 0x80, IntPtr.Zero);
        if (result == new IntPtr(-1))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot open standard stream.");
        return result;
    }

    public static IntPtr InheritableCopy(IntPtr source)
    {
        IntPtr copy;
        if (!DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(), out copy, 0, true, 2))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot prepare supervisor settings output.");
        return copy;
    }

    public static uint ActiveJobProcesses(IntPtr job)
    {
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info;
        if (!QueryInformationJobObject(job, 1, out info,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)), IntPtr.Zero))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect the host process group.");
        return info.ActiveProcesses;
    }

    public static bool LiveJobProcess(uint pid, IntPtr job)
    {
        IntPtr process = OpenProcess(0x1000 | SYNCHRONIZE, false, pid);
        if (process == IntPtr.Zero) return false;
        try
        {
            bool member;
            return WaitForSingleObject(process, 0) == WAIT_TIMEOUT &&
                IsProcessInJob(process, job, out member) && member;
        }
        finally { CloseHandle(process); }
    }

    // Windows argv quoting, including paths ending in a backslash.
    public static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(character);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }
}
'@
}

function Throw-JobError([string]$Message, [int]$Code) {
    throw (New-Object ComponentModel.Win32Exception($Code, $Message))
}

function New-HostJob {
    $handle = [AutoColabConsoleJobNative]::CreateJobObject([IntPtr]::Zero, $null)
    if ($handle -eq [IntPtr]::Zero) {
        Throw-JobError 'Cannot create the host process group.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    try {
        if (-not [AutoColabConsoleJobNative]::SetHandleInformation($handle, 1, 0)) {
            Throw-JobError 'Cannot protect the supervisor job handle.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        $limits = New-Object AutoColabConsoleJobNative+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        $basic = $limits.BasicLimitInformation
        $basic.LimitFlags = [AutoColabConsoleJobNative]::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        $limits.BasicLimitInformation = $basic
        $limitsSize = [Runtime.InteropServices.Marshal]::SizeOf($limits)
        $pointer = [Runtime.InteropServices.Marshal]::AllocHGlobal($limitsSize)
        try {
            [Runtime.InteropServices.Marshal]::StructureToPtr($limits, $pointer, $false)
            if (-not [AutoColabConsoleJobNative]::SetInformationJobObject($handle, 9, $pointer, [uint32]$limitsSize)) {
                Throw-JobError 'Cannot configure automatic host shutdown.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
        }
        finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($pointer) }
        return $handle
    }
    catch {
        [void][AutoColabConsoleJobNative]::CloseHandle($handle)
        throw
    }
}

function Stop-HostJob([IntPtr]$Handle) {
    # Drain every descendant before creating the next job: an old CLI must never
    # overlap the next attempt. Closing the handle remains the crash safety net.
    if (-not [AutoColabConsoleJobNative]::TerminateJobObject($Handle, 1)) {
        Throw-JobError 'Cannot stop the old host process group.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    while ([AutoColabConsoleJobNative]::ActiveJobProcesses($Handle) -gt 0) {
        if ($deadline.Elapsed.TotalSeconds -ge 10) {
            throw 'The old host process group did not stop; a new host was not started.'
        }
        Start-Sleep -Milliseconds 50
    }
}

function Read-HostProgress([string]$Path, [IntPtr]$Job, [DateTimeOffset]$AttemptStarted) {
    try {
        # The status file is local telemetry. Locked/partial/missing status is
        # ignored until the watchdog deadline, rather than crashing supervision.
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        try {
            if ($stream.Length -gt 131072) { return $null }
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
            try { $status = $reader.ReadToEnd() | ConvertFrom-Json }
            finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
        if (-not $status.instance_id -or $status.pid -isnot [int] -or $status.pid -le 0) { return $null }
        $started = [DateTimeOffset]::Parse($status.started_at)
        $updated = [DateTimeOffset]::Parse($status.updated_at)
        if ($started -lt $AttemptStarted -or $updated -lt $started) { return $null }
        if (-not [AutoColabConsoleJobNative]::LiveJobProcess([uint32]$status.pid, $Job)) { return $null }
        return @{ instance_id = [string]$status.instance_id; updated_at = [string]$status.updated_at }
    }
    catch { return $null }
}

function Get-SupervisorSettings([IntPtr]$Parent) {
    # Configuration is read before launch, but remains bounded and owned by a
    # job too. Closing the console during this step leaves no helper behind.
    $queryJob = [IntPtr]::Zero
    $queryHandles = @()
    $queryProcess = New-Object AutoColabConsoleJobNative+PROCESS_INFORMATION
    $queryFile = [IO.Path]::GetTempFileName()
    $queryStream = $null
    try {
        $queryJob = New-HostJob
        $queryStream = [IO.File]::Open($queryFile, [IO.FileMode]::Create,
            [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
        $queryHandles = @([AutoColabConsoleJobNative]::InheritableStandardHandle(-10),
            [AutoColabConsoleJobNative]::InheritableCopy($queryStream.SafeFileHandle.DangerousGetHandle()))
        $startup = New-Object AutoColabConsoleJobNative+STARTUPINFO
        $startup.cb = [Runtime.InteropServices.Marshal]::SizeOf($startup)
        $startup.dwFlags = [AutoColabConsoleJobNative]::STARTF_USESTDHANDLES
        $startup.hStdInput = $queryHandles[0]
        $startup.hStdOutput = $startup.hStdError = $queryHandles[1]
        $queryCommand = [AutoColabConsoleJobNative]::Quote($taskPython) + ' -X utf8 ' +
            [AutoColabConsoleJobNative]::Quote((Join-Path $PSScriptRoot 'config.py')) +
            ' --launcher-settings ' + [AutoColabConsoleJobNative]::Quote($taskConfig)
        $commandLine = New-Object Text.StringBuilder($queryCommand)
        if (-not [AutoColabConsoleJobNative]::CreateProcess($taskPython, $commandLine,
                [IntPtr]::Zero, [IntPtr]::Zero, $true,
                [AutoColabConsoleJobNative]::CREATE_SUSPENDED, [IntPtr]::Zero,
                $PSScriptRoot, [ref]$startup, [ref]$queryProcess)) {
            Throw-JobError 'Cannot read supervisor settings.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        foreach ($handle in $queryHandles) { [void][AutoColabConsoleJobNative]::CloseHandle($handle) }
        $queryHandles = @()
        if (-not [AutoColabConsoleJobNative]::AssignProcessToJobObject($queryJob, $queryProcess.hProcess)) {
            $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            [void][AutoColabConsoleJobNative]::TerminateProcess($queryProcess.hProcess, 1)
            Throw-JobError 'Cannot own the supervisor settings helper.' $code
        }
        if ([AutoColabConsoleJobNative]::ResumeThread($queryProcess.hThread) -eq [uint32]::MaxValue) {
            Throw-JobError 'Cannot start the supervisor settings helper.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $waitHandles = [IntPtr[]]@($Parent, $queryProcess.hProcess)
        while ($true) {
            $wait = [AutoColabConsoleJobNative]::WaitForMultipleObjects(2, $waitHandles, $false, 500)
            if ($wait -eq 0) { return @{ exit_code = 0; parent_closed = $true; output = '' } }
            if ($wait -eq 1) {
                [uint32]$code = 0
                if (-not [AutoColabConsoleJobNative]::GetExitCodeProcess($queryProcess.hProcess, [ref]$code)) {
                    Throw-JobError 'Cannot read supervisor settings exit code.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
                }
                $queryStream.Dispose()
                $queryStream = $null
                return @{ exit_code = $code; parent_closed = $false; output = [IO.File]::ReadAllText($queryFile, [Text.Encoding]::UTF8) }
            }
            if ($wait -ne [AutoColabConsoleJobNative]::WAIT_TIMEOUT) {
                Throw-JobError 'Cannot wait for supervisor settings.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
            if ($clock.Elapsed.TotalSeconds -ge 30) { throw 'Reading supervisor settings timed out after 30 seconds.' }
        }
    }
    finally {
        try { if ($queryJob -ne [IntPtr]::Zero) { Stop-HostJob $queryJob } }
        finally {
            if ($queryJob -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($queryJob) }
            if ($queryProcess.hThread -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($queryProcess.hThread) }
            if ($queryProcess.hProcess -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($queryProcess.hProcess) }
            foreach ($handle in $queryHandles) { [void][AutoColabConsoleJobNative]::CloseHandle($handle) }
            if ($queryStream) { $queryStream.Dispose() }
            try { [IO.File]::Delete($queryFile) }
            catch { Write-Warning "Cannot remove settings query temporary file: $queryFile" }
        }
    }
}

if (-not $PythonPath) {
    $PythonPath = @(Get-Command python -CommandType Application -ErrorAction Stop)[0].Source
}
$taskPython = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
$taskBootstrap = Join-Path $PSScriptRoot 'bootstrap.py'
if (-not (Test-Path -LiteralPath $taskBootstrap -PathType Leaf)) {
    throw "Missing environment bootstrap: $taskBootstrap"
}
if (-not $ConfigFile) { $ConfigFile = Join-Path $PSScriptRoot 'config.toml' }
$taskConfig = (Resolve-Path -LiteralPath $ConfigFile -ErrorAction Stop).Path

$jobHandle = [IntPtr]::Zero
$parentHandle = [IntPtr]::Zero
$standardHandles = @()
$processInfo = New-Object AutoColabConsoleJobNative+PROCESS_INFORMATION
$taskExitCode = 1
$supervisorMutex = $null
$supervisorOwned = $false

try {
    # Keep an actual process handle, rather than checking a PID that may be reused.
    $parentId = [AutoColabConsoleJobNative]::ParentProcessId()
    $parentHandle = [AutoColabConsoleJobNative]::OpenProcess(
        [AutoColabConsoleJobNative]::SYNCHRONIZE, $false, $parentId)
    if ($parentHandle -eq [IntPtr]::Zero) {
        Throw-JobError 'Cannot monitor the launcher; host was not started.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }

    # Keep ownership throughout retries, when the worker mutex is temporarily
    # free. Another console must not take over between this session's attempts.
    try {
        $supervisorMutex = New-Object Threading.Mutex($false, 'Global\AutoColabConsoleSupervisor_v1')
        try { $supervisorOwned = $supervisorMutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $supervisorOwned = $true }
    }
    catch [UnauthorizedAccessException] {
        Write-Host '[AutoColab] Another console supervisor is running, or mutex access is denied.'
        exit 2
    }
    if (-not $supervisorOwned) {
        Write-Host '[AutoColab] Another AutoColab console supervisor is already running.'
        exit 2
    }

    $query = Get-SupervisorSettings $parentHandle
    if ($query.parent_closed) { exit 0 }
    if ($query.exit_code -ne 0) {
        Write-Host $query.output
        Write-Host '[AutoColab] Fix the configuration and start again.'
        exit $query.exit_code
    }
    $supervisor = $query.output | ConvertFrom-Json
    $statusPath = Join-Path $supervisor.runtime_dir 'status.json'
    $restartDelay = [double]$supervisor.restart_initial_seconds

    Write-Host '[AutoColab] Logs appear here. Closing this window stops the host and its children.'
    $parentClosed = $false
    while (-not $parentClosed) {
        if ([AutoColabConsoleJobNative]::WaitForSingleObject($parentHandle, 0) -eq 0) { break }
        $jobHandle = New-HostJob
        $processInfo = New-Object AutoColabConsoleJobNative+PROCESS_INFORMATION
        $attemptStarted = [DateTimeOffset]::UtcNow
        $attemptClock = [Diagnostics.Stopwatch]::StartNew()
        $lastProgressSeconds = 0.0
        $lastProgressIdentity = ''
        $failureReason = ''
        try {
            $startup = New-Object AutoColabConsoleJobNative+STARTUPINFO
            $startup.cb = [Runtime.InteropServices.Marshal]::SizeOf($startup)
            $startup.dwFlags = [AutoColabConsoleJobNative]::STARTF_USESTDHANDLES
            foreach ($kind in @(-10, -11, -12)) {
                $standardHandles += [AutoColabConsoleJobNative]::InheritableStandardHandle($kind)
            }
            $startup.hStdInput, $startup.hStdOutput, $startup.hStdError = $standardHandles
            $taskCommand = [AutoColabConsoleJobNative]::Quote($taskPython) +
                ' -X utf8 -u ' + [AutoColabConsoleJobNative]::Quote($taskBootstrap) +
                ' --config ' + [AutoColabConsoleJobNative]::Quote($taskConfig)
            $commandLine = New-Object Text.StringBuilder($taskCommand)

            # Suspension closes the race in which bootstrap could spawn an unowned child.
            if (-not [AutoColabConsoleJobNative]::CreateProcess($taskPython, $commandLine,
                    [IntPtr]::Zero, [IntPtr]::Zero, $true,
                    [AutoColabConsoleJobNative]::CREATE_SUSPENDED, [IntPtr]::Zero,
                    $PSScriptRoot, [ref]$startup, [ref]$processInfo)) {
                Throw-JobError 'Cannot start the environment bootstrap.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
            foreach ($handle in $standardHandles) { [void][AutoColabConsoleJobNative]::CloseHandle($handle) }
            $standardHandles = @()
            if (-not [AutoColabConsoleJobNative]::AssignProcessToJobObject($jobHandle, $processInfo.hProcess)) {
                $failureCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                [void][AutoColabConsoleJobNative]::TerminateProcess($processInfo.hProcess, 1)
                Throw-JobError 'Cannot assign the host to its process group.' $failureCode
            }
            if ([AutoColabConsoleJobNative]::ResumeThread($processInfo.hThread) -eq [uint32]::MaxValue) {
                Throw-JobError 'Cannot resume the environment bootstrap.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
            [void][AutoColabConsoleJobNative]::CloseHandle($processInfo.hThread)
            $processInfo.hThread = [IntPtr]::Zero

            Write-Host '[AutoColab] Preparing environment and starting host...'
            $waitHandles = [IntPtr[]]@($parentHandle, $processInfo.hProcess)
            while ($true) {
                $waitResult = [AutoColabConsoleJobNative]::WaitForMultipleObjects(2, $waitHandles, $false, 500)
                if ($waitResult -eq 0) {
                    $taskExitCode = 0
                    $parentClosed = $true
                    break
                }
                if ($waitResult -eq 1) {
                    [uint32]$childExitCode = 0
                    if (-not [AutoColabConsoleJobNative]::GetExitCodeProcess($processInfo.hProcess, [ref]$childExitCode)) {
                        Throw-JobError 'Cannot read the host exit code.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
                    }
                    $taskExitCode = [BitConverter]::ToInt32([BitConverter]::GetBytes($childExitCode), 0)
                    $failureReason = "Host exited with code $taskExitCode."
                    break
                }
                if ($waitResult -ne [AutoColabConsoleJobNative]::WAIT_TIMEOUT) {
                    Throw-JobError 'Cannot wait for the host or launcher.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
                }
                $progress = Read-HostProgress $statusPath $jobHandle $attemptStarted
                if ($progress) {
                    $identity = $progress.instance_id + ':' + $progress.updated_at
                    if ($identity -ne $lastProgressIdentity) {
                        $lastProgressIdentity = $identity
                        $lastProgressSeconds = $attemptClock.Elapsed.TotalSeconds
                    }
                }
                if ($attemptClock.Elapsed.TotalSeconds - $lastProgressSeconds -ge [double]$supervisor.watchdog_seconds) {
                    $taskExitCode = 1
                    $failureReason = "No valid host progress for $($supervisor.watchdog_seconds) seconds; restarting the process group."
                    break
                }
            }
        }
        finally {
            # A fresh job is used on every restart, after this job has drained.
            try { Stop-HostJob $jobHandle }
            finally {
                [void][AutoColabConsoleJobNative]::CloseHandle($jobHandle)
                $jobHandle = [IntPtr]::Zero
                if ($processInfo.hThread -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($processInfo.hThread) }
                if ($processInfo.hProcess -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($processInfo.hProcess) }
                $processInfo = New-Object AutoColabConsoleJobNative+PROCESS_INFORMATION
                foreach ($handle in $standardHandles) { [void][AutoColabConsoleJobNative]::CloseHandle($handle) }
                $standardHandles = @()
            }
        }
        # A graceful stop (0), duplicate guard (2), or invalid config (3) needs no restart.
        if ($parentClosed -or $taskExitCode -in @(0, 2, 3)) { break }
        if ($attemptClock.Elapsed.TotalSeconds -ge [double]$supervisor.restart_reset_seconds) {
            $restartDelay = [double]$supervisor.restart_initial_seconds
        }
        Write-Host "[AutoColab] $failureReason Retry in $restartDelay seconds."
        $backoffClock = [Diagnostics.Stopwatch]::StartNew()
        while ($backoffClock.Elapsed.TotalSeconds -lt $restartDelay) {
            $remainingMs = [Math]::Ceiling(($restartDelay - $backoffClock.Elapsed.TotalSeconds) * 1000)
            $wait = [AutoColabConsoleJobNative]::WaitForSingleObject($parentHandle, [uint32][Math]::Min(500, $remainingMs))
            if ($wait -eq 0) { $parentClosed = $true; $taskExitCode = 0; break }
            if ($wait -ne [AutoColabConsoleJobNative]::WAIT_TIMEOUT) {
                Throw-JobError 'Cannot monitor the launcher during retry.' ([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
        }
        $restartDelay = [Math]::Min([double]$supervisor.restart_max_seconds, $restartDelay * 2)
    }
}
finally {
    # Closing the sole job handle kills bootstrap, the .venv host, Codex and all
    # remaining descendants, even when bootstrap already returned an error.
    if ($jobHandle -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($jobHandle) }
    if ($processInfo.hThread -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($processInfo.hThread) }
    if ($processInfo.hProcess -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($processInfo.hProcess) }
    if ($parentHandle -ne [IntPtr]::Zero) { [void][AutoColabConsoleJobNative]::CloseHandle($parentHandle) }
    foreach ($handle in $standardHandles) { [void][AutoColabConsoleJobNative]::CloseHandle($handle) }
    if ($supervisorMutex) {
        try { if ($supervisorOwned) { $supervisorMutex.ReleaseMutex() } }
        finally { $supervisorMutex.Dispose() }
    }
}

exit $taskExitCode
