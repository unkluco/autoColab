"""Per-call Windows job; a gated launcher cannot spawn before assignment."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
from ctypes import wintypes


class WindowsJob:
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        size = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [('user', ctypes.c_int64), ('job_user', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('minimum', size), ('maximum', size),
                        ('active_limit', wintypes.DWORD), ('affinity', size),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', size),
                        ('job_memory', size), ('peak_process', size), ('peak_job', size)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (name, wintypes.DWORD) for name in ('faults', 'total', 'active', 'terminated')]
        self.accounting_type = Accounting
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        declarations = {
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self):
        information = self.accounting_type()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(information),
                                                     ctypes.sizeof(information), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return information.active

    def stop(self):
        if not self.handle:
            return
        if self.active() and not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 5
        while self.active():
            if time.monotonic() >= deadline:
                raise OSError('Codex call process job did not drain')
            time.sleep(.02)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def gated_command(command):
    return [sys.executable, '-I', '-S', '-u', str(Path(__file__).resolve()), '--gate', *command]


if __name__ == '__main__':
    if sys.argv[1:2] != ['--gate']:
        raise SystemExit(2)
    # os.read must not prefetch any notebook bytes destined for the CLI.
    if os.read(0, 1) != b'\0':
        raise SystemExit(1)
    child = subprocess.Popen(sys.argv[2:])
    raise SystemExit(child.wait())
