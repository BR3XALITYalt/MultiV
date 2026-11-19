"""Hidden Windows message window for graceful service-manager shutdown."""

from __future__ import annotations

import ntpath
import os
import threading
from typing import Any

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
SW_HIDE = 0

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


def _open_nssm_parent_process() -> tuple[Any, Any, int] | None:
    """Open the immediate parent only when it is the NSSM service wrapper."""
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    parent_pid = 0
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())

        current_pid = int(kernel32.GetCurrentProcessId())
        while True:
            if int(entry.th32ProcessID) == current_pid:
                parent_pid = int(entry.th32ParentProcessID)
                break
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)

    if parent_pid <= 0 or parent_pid == os.getpid():
        return None

    parent_handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
        False,
        parent_pid,
    )
    if not parent_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        image_buffer = ctypes.create_unicode_buffer(32768)
        image_length = wintypes.DWORD(len(image_buffer))
        if not kernel32.QueryFullProcessImageNameW(
            parent_handle,
            0,
            image_buffer,
            ctypes.byref(image_length),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        image_path = image_buffer.value
        if ntpath.basename(image_path).casefold() != "nssm.exe":
            kernel32.CloseHandle(parent_handle)
            parent_handle = None
            return None

        # Return the open handle so the PID cannot be recycled before termination.
        result = (kernel32, parent_handle, parent_pid)
        parent_handle = None
        return result
    finally:
        if parent_handle:
            kernel32.CloseHandle(parent_handle)


def stop_nssm_parent_process(logger: Any) -> bool:
    """Stop only an NSSM process that is the direct parent of this process."""
    if os.name != "nt":
        return False

    import ctypes

    try:
        parent = _open_nssm_parent_process()
    except Exception as exc:
        logger.warning("Could not inspect the parent process for NSSM shutdown: %s", exc)
        return False

    if parent is None:
        logger.info("NSSM stop was detected, but the parent is not nssm.exe; leaving it untouched.")
        return False

    kernel32, parent_handle, parent_pid = parent
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    try:
        if not kernel32.TerminateProcess(parent_handle, 0):
            error_code = ctypes.get_last_error()
            wait_result = kernel32.WaitForSingleObject(parent_handle, 0)
            if wait_result == WAIT_OBJECT_0:
                logger.info("NSSM parent process %d had already exited.", parent_pid)
                return True
            logger.warning(
                "Could not terminate NSSM parent process %d (Windows error %d).",
                parent_pid,
                error_code,
            )
            return False

        wait_result = kernel32.WaitForSingleObject(parent_handle, 5000)
        if wait_result == WAIT_OBJECT_0:
            logger.info("Terminated NSSM parent process %d after graceful MultiV shutdown.", parent_pid)
            return True
        if wait_result == WAIT_TIMEOUT:
            logger.warning("NSSM parent process %d did not exit after termination.", parent_pid)
        else:
            logger.warning(
                "Could not confirm NSSM parent process %d termination (wait result 0x%08X).",
                parent_pid,
                wait_result,
            )
        return False
    finally:
        kernel32.CloseHandle(parent_handle)


def hide_service_console(logger: Any) -> bool:
    """Hide the NSSM-created console without detaching from its control events."""
    if os.name != "nt":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL

        console_window = kernel32.GetConsoleWindow()
        if not console_window:
            return False

        user32.ShowWindow(console_window, SW_HIDE)
        logger.info("NSSM console hidden; service control events remain enabled.")
        return True
    except Exception as exc:
        logger.warning("Could not hide the NSSM console: %s", exc)
        return False


def handle_service_stop_message(
    message: int,
    shutdown_event: threading.Event,
    logger: Any,
    *,
    nssm_stop_event: threading.Event | None = None,
) -> bool:
    """Turn a service-manager window or thread close message into shutdown."""
    if message not in {WM_CLOSE, WM_QUIT}:
        return False

    if nssm_stop_event is not None:
        nssm_stop_event.set()
    if not shutdown_event.is_set():
        logger.info(
            "Received Windows service shutdown message (0x%04X); stopping MultiV.",
            message,
        )
        shutdown_event.set()
    return True


class WindowsServiceStopWindow:
    """Expose a hidden top-level window that NSSM can close without a console."""

    def __init__(
        self,
        shutdown_event: threading.Event,
        logger: Any,
        nssm_stop_event: threading.Event | None = None,
    ) -> None:
        self.shutdown_event = shutdown_event
        self.logger = logger
        self.nssm_stop_event = nssm_stop_event
        self._closing = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: Any = None
        self._startup_error: BaseException | None = None
        self._wndproc: Any = None
        self._user32: Any = None

    def start(self, timeout: float = 5.0) -> bool:
        """Create the hidden window on Windows; do nothing on other platforms."""
        if os.name != "nt":
            return False
        if self._thread is not None:
            return self._hwnd is not None

        self._thread = threading.Thread(
            target=self._run,
            name="txitpy-service-stop-window",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(max(0.1, float(timeout))):
            raise TimeoutError("The hidden Windows service-stop window did not start.")
        if self._startup_error is not None:
            raise RuntimeError(
                f"Could not create the hidden Windows service-stop window: {self._startup_error}"
            ) from self._startup_error
        return self._hwnd is not None

    def close(self, timeout: float = 2.0) -> None:
        """Remove the hidden window and wait briefly for its message thread."""
        self._closing = True
        hwnd = self._hwnd
        user32 = self._user32
        if hwnd and user32 is not None:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            lresult = ctypes.c_ssize_t
            wndproc_type = ctypes.WINFUNCTYPE(
                lresult,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            class WindowClass(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", wndproc_type),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HANDLE),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HANDLE),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._user32 = user32

            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
            user32.RegisterClassW.restype = ctypes.c_ushort
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HANDLE,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.DefWindowProcW.restype = lresult
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            user32.UnregisterClassW.restype = wintypes.BOOL

            instance = kernel32.GetModuleHandleW(None)
            if not instance:
                raise ctypes.WinError(ctypes.get_last_error())

            class_name = f"TXITPY_ServiceStop_{os.getpid()}_{threading.get_ident()}"

            def window_proc(hwnd, message, wparam, lparam):
                if message == WM_CLOSE:
                    if not self._closing:
                        handle_service_stop_message(
                            message,
                            self.shutdown_event,
                            self.logger,
                            nssm_stop_event=self.nssm_stop_event,
                        )
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._wndproc = wndproc_type(window_proc)
            window_class = WindowClass()
            window_class.style = 0
            window_class.lpfnWndProc = self._wndproc
            window_class.cbClsExtra = 0
            window_class.cbWndExtra = 0
            window_class.hInstance = instance
            window_class.hIcon = None
            window_class.hCursor = None
            window_class.hbrBackground = None
            window_class.lpszMenuName = None
            window_class.lpszClassName = class_name

            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())

            # No WS_VISIBLE style and no ShowWindow call: NSSM can enumerate
            # and close this top-level window, but it is never displayed.
            hwnd = user32.CreateWindowExW(
                0,
                class_name,
                "MultiV service control",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                error = ctypes.get_last_error()
                user32.UnregisterClassW(class_name, instance)
                raise ctypes.WinError(error)

            self._hwnd = hwnd
            self._ready.set()
            self.logger.info(
                "Hidden Windows service-stop window ready; NSSM can stop MultiV without a console."
            )

            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    # NSSM may post WM_QUIT after closing the hidden window.
                    # Do not mistake our own cleanup message for a service stop.
                    if not self._closing:
                        handle_service_stop_message(
                            WM_QUIT,
                            self.shutdown_event,
                            self.logger,
                            nssm_stop_event=self.nssm_stop_event,
                        )
                    break
                if result < 0:
                    raise ctypes.WinError(ctypes.get_last_error())
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))

            self._hwnd = None
            user32.UnregisterClassW(class_name, instance)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            if not self.shutdown_event.is_set():
                self.logger.warning(
                    "Hidden Windows service-stop window failed: %s",
                    exc,
                )
        finally:
            self._hwnd = None
