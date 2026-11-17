"""Hidden Windows message window for graceful service-manager shutdown."""

from __future__ import annotations

import os
import threading
from typing import Any

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012


def handle_service_stop_message(
    message: int,
    shutdown_event: threading.Event,
    logger: Any,
) -> bool:
    """Turn a service-manager window or thread close message into shutdown."""
    if message not in {WM_CLOSE, WM_QUIT}:
        return False

    if not shutdown_event.is_set():
        logger.info(
            "Received Windows service shutdown message (0x%04X); stopping TXITPY.",
            message,
        )
        shutdown_event.set()
    return True


class WindowsServiceStopWindow:
    """Expose a hidden top-level window that NSSM can close without a console."""

    def __init__(self, shutdown_event: threading.Event, logger: Any) -> None:
        self.shutdown_event = shutdown_event
        self.logger = logger
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
                    handle_service_stop_message(message, self.shutdown_event, self.logger)
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
                "TXITPY service control",
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
                "Hidden Windows service-stop window ready; NSSM can stop TXITPY without a console."
            )

            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    # NSSM's thread shutdown stage posts WM_QUIT. The event
                    # may already be set when WM_CLOSE destroyed the window.
                    handle_service_stop_message(WM_QUIT, self.shutdown_event, self.logger)
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
