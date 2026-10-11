"""Own Windows CSS descendants independently of taskkill and parent lifetime."""
import ctypes
from ctypes import wintypes
import uuid

JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "user_time", "kernel_time", "period_user_time", "period_kernel_time"
    )] + [(name, wintypes.DWORD) for name in (
        "page_faults", "total", "active", "terminated"
    )]


class Limits(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class WindowsCssJob:
    def __init__(self):
        self.name = "financial-planner-css-" + uuid.uuid4().hex
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self._check(self.api.CreateJobObjectW(None, self.name))
        try:
            limits = ExtendedLimits()
            # KILL_ON_JOB_CLOSE, with no breakaway permission. If the launcher
            # itself exits, its last job handle still stops every descendant.
            limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._check(self.api.SetInformationJobObject(
                self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def assign(self, pid):
        # SET_QUOTA | TERMINATE: the rights required by AssignProcessToJobObject.
        handle = self._check(self.api.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid))
        try:
            self._check(self.api.AssignProcessToJobObject(self.handle, handle))
        finally:
            self._check(self.api.CloseHandle(handle))

    def terminate(self):
        self._check(self.api.TerminateJobObject(self.handle, 1))

    def active(self):
        accounting = Accounting()
        self._check(self.api.QueryInformationJobObject(
            self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return accounting.active

    def pids(self):
        # Bounded diagnostic snapshot, not a PID-based kill list. A busy job
        # exceeding this buffer fails closed rather than truncating ownership.
        class ProcessList(ctypes.Structure):
            _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                        ("ids", ctypes.c_size_t * 4096)]

        processes = ProcessList()
        self._check(self.api.QueryInformationJobObject(
            self.handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(processes), ctypes.sizeof(processes), None))
        return list(processes.ids[:processes.count])

    def close(self):
        if self.handle:
            self._check(self.api.CloseHandle(self.handle))
            self.handle = None
