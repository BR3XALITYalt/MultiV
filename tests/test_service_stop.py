import logging
import threading
import unittest
from unittest.mock import patch

from service_stop import (
    WM_CLOSE,
    WM_DESTROY,
    WM_QUIT,
    handle_service_stop_message,
    hide_service_console,
    stop_nssm_parent_process,
)


class WindowsServiceStopTests(unittest.TestCase):
    def test_console_hiding_is_skipped_off_windows(self):
        logger = logging.getLogger("test.service-stop-console")
        with patch("service_stop.os.name", "posix"):
            self.assertFalse(hide_service_console(logger))

    def test_wm_close_requests_graceful_shutdown(self):
        shutdown_event = threading.Event()
        nssm_stop_event = threading.Event()
        logger = logging.getLogger("test.service-stop")

        with self.assertLogs(logger, level="INFO") as captured:
            handled = handle_service_stop_message(
                WM_CLOSE,
                shutdown_event,
                logger,
                nssm_stop_event=nssm_stop_event,
            )

        self.assertTrue(handled)
        self.assertTrue(shutdown_event.is_set())
        self.assertTrue(nssm_stop_event.is_set())
        self.assertIn("Windows service shutdown message", "\n".join(captured.output))

    def test_wm_quit_requests_graceful_shutdown(self):
        shutdown_event = threading.Event()
        logger = logging.getLogger("test.service-stop-quit")

        self.assertTrue(
            handle_service_stop_message(WM_QUIT, shutdown_event, logger)
        )
        self.assertTrue(shutdown_event.is_set())

    def test_nssm_stop_does_not_terminate_parent_off_windows(self):
        logger = logging.getLogger("test.service-stop-parent")
        with patch("service_stop.os.name", "posix"), patch(
            "service_stop._open_nssm_parent_process"
        ) as open_parent:
            self.assertFalse(stop_nssm_parent_process(logger))
        open_parent.assert_not_called()

    def test_nssm_parent_is_terminated_and_waited_for(self):
        class FakeKernel32:
            def __init__(self):
                self.terminated = []
                self.waited = []
                self.closed = []

            def TerminateProcess(self, handle, exit_code):
                self.terminated.append((handle, exit_code))
                return 1

            def WaitForSingleObject(self, handle, timeout):
                self.waited.append((handle, timeout))
                return 0

            def CloseHandle(self, handle):
                self.closed.append(handle)
                return 1

        logger = logging.getLogger("test.service-stop-parent")
        api = FakeKernel32()
        with patch("service_stop.os.name", "nt"), patch(
            "service_stop._open_nssm_parent_process",
            return_value=(api, 1234, 5678),
        ):
            self.assertTrue(stop_nssm_parent_process(logger))

        self.assertEqual(api.terminated, [(1234, 0)])
        self.assertEqual(api.waited, [(1234, 5000)])
        self.assertEqual(api.closed, [1234])

    def test_unrelated_window_messages_are_ignored(self):
        shutdown_event = threading.Event()
        logger = logging.getLogger("test.service-stop-ignore")

        self.assertFalse(
            handle_service_stop_message(WM_DESTROY, shutdown_event, logger)
        )
        self.assertFalse(shutdown_event.is_set())


if __name__ == "__main__":
    unittest.main()
