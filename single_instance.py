"""One AutoColab host per machine, independent of project/config/runtime paths."""
from __future__ import annotations

from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

DEFAULT_NAME = r'Global\AutoColabHost_v1'
if os.name == 'nt':
    _state_root = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
else:
    _state_root = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state')))
OWNER_PATH = _state_root / 'AutoColab' / 'host.json'


class DuplicateInstance(RuntimeError):
    """Another host owns the machine mutex, or its access is denied."""


def _kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.ReleaseMutex.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return kernel


class _WindowsMutex:
    def __init__(self, name):
        self.name = name
        self.kernel = _kernel32()
        self.handle = None
        self.owned = False

    def acquire(self):
        self.handle = self.kernel.CreateMutexW(None, False, self.name)
        if not self.handle:
            code = ctypes.get_last_error()
            if code == 5:  # ERROR_ACCESS_DENIED: never fall back to a weaker lock.
                raise DuplicateInstance('Another AutoColab host is running, or its mutex access is denied.')
            raise ctypes.WinError(code)
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            self.owned = True
            return
        code = ctypes.get_last_error() if result == 0xFFFFFFFF else None
        self.release()
        if result == 0x102 or code == 5:  # WAIT_TIMEOUT
            raise DuplicateInstance('Another AutoColab host is already running on this machine.')
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(code)
        raise OSError(f'Unexpected mutex wait result: {result}')

    def release(self):
        if self.handle:
            try:
                if self.owned:
                    self.kernel.ReleaseMutex(self.handle)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                self.owned = False


class _PortableLock:
    def __init__(self, name, owner_path):
        suffix = hashlib.sha256(name.encode('utf-8')).hexdigest()[:24]
        self.path = owner_path.parent / f'host-{suffix}.lock'
        self.stream = None

    def acquire(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.stream.close()
            self.stream = None
            raise DuplicateInstance('Another AutoColab host is already running for this user.') from exc
        except BaseException:
            self.stream.close()
            self.stream = None
            raise

    def release(self):
        if self.stream is not None:
            import fcntl
            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


def _make_lock(name, owner_path):
    return _WindowsMutex(name) if os.name == 'nt' else _PortableLock(name, owner_path)


def _process_start_id(pid):
    """Validate a live PID against its creation identity, avoiding PID reuse."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == 'nt':
        class FileTime(ctypes.Structure):
            _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]

        kernel = _kernel32()
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(FileTime)] * 4)]
        kernel.GetProcessTimes.restype = ctypes.c_int
        process = kernel.OpenProcess(0x1000 | 0x100000, False, pid)  # Query limited + synchronize.
        if not process:
            return None
        try:
            if kernel.WaitForSingleObject(process, 0) != 0x102:
                return None
            created, exited, cpu_kernel, cpu_user = (FileTime() for _ in range(4))
            if not kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
                return None
            return f'windows:{(created.high << 32) | created.low}'
        finally:
            kernel.CloseHandle(process)
    try:
        if Path('/proc').is_dir():
            # The name can contain spaces/parentheses; fields begin after the final ')'.
            fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                return None
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return f'linux:{boot}:{fields[19]}'  # starttime is field 22.
        os.kill(pid, 0)
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                                text=True, timeout=5)
        return f'posix:{result.stdout.strip()}' if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return None


def _read_owner(path):
    try:
        owner = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(owner, dict):
            return None
        if not all(isinstance(owner.get(key), str) and owner[key] for key in
                   ('instance_id', 'runtime_dir', 'watch_folder', 'process_start_id', 'started_at')):
            return None
        if not Path(owner['runtime_dir']).is_absolute() or not Path(owner['watch_folder']).is_absolute():
            return None
        if _process_start_id(owner.get('pid')) != owner['process_start_id']:
            return None
        return owner
    except (OSError, ValueError, TypeError):
        return None


def _publish_owner(path, owner):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='host-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(owner, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class MachineInstance:
    """Hold the machine mutex and publish where its active host can be controlled."""
    def __init__(self, runtime_dir: Path, watch_folder: Path, name=DEFAULT_NAME, owner_path=OWNER_PATH):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.watch_folder = Path(watch_folder).resolve()
        self.owner_path = Path(owner_path).resolve()
        self.lock = _make_lock(name, self.owner_path)
        self.owner = None
        self.acquired = False

    def acquire(self):
        if self.acquired:
            raise RuntimeError('This MachineInstance already owns the mutex.')
        self.lock.acquire()
        self.acquired = True
        try:
            # Clear a previous run's stop request before controllers can see this owner.
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            (self.runtime_dir / 'stop.request').unlink(missing_ok=True)
            start_id = _process_start_id(os.getpid())
            if start_id is None:
                raise RuntimeError('Cannot determine AutoColab process creation identity.')
            self.owner = {
                'pid': os.getpid(), 'runtime_dir': str(self.runtime_dir),
                'watch_folder': str(self.watch_folder), 'instance_id': str(uuid.uuid4()),
                'started_at': datetime.now(timezone.utc).isoformat(),
                'process_start_id': start_id,
            }
            _publish_owner(self.owner_path, self.owner)
            return self
        except BaseException:
            self.release()
            raise

    def release(self):
        if not self.acquired:
            return
        try:
            # Never remove a descriptor written by a different acquisition/run.
            if self.owner:
                try:
                    current = json.loads(self.owner_path.read_text(encoding='utf-8'))
                    if isinstance(current, dict) and current.get('instance_id') == self.owner['instance_id']:
                        self.owner_path.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError):
                    pass
        finally:
            self.lock.release()
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def machine_status(name=DEFAULT_NAME, owner_path=OWNER_PATH):
    """Return mutex state and only a validated live owner descriptor."""
    owner_path = Path(owner_path).resolve()
    lock = _make_lock(name, owner_path)
    try:
        lock.acquire()
    except DuplicateInstance:
        return True, _read_owner(owner_path)
    else:
        # A stale descriptor is not evidence of a running host.
        lock.release()
        return False, None
