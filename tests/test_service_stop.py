import logging
import threading
import unittest

from service_stop import (
    WM_CLOSE,
    WM_DESTROY,
    WM_QUIT,
    handle_service_stop_message,
)


class WindowsServiceStopTests(unittest.TestCase):
    def test_wm_close_requests_graceful_shutdown(self):
        shutdown_event = threading.Event()
        logger = logging.getLogger("test.service-stop")

        with self.assertLogs(logger, level="INFO") as captured:
            handled = handle_service_stop_message(
                WM_CLOSE,
                shutdown_event,
                logger,
            )

        self.assertTrue(handled)
        self.assertTrue(shutdown_event.is_set())
        self.assertIn("Windows service shutdown message", "\n".join(captured.output))

    def test_wm_quit_requests_graceful_shutdown(self):
        shutdown_event = threading.Event()
        logger = logging.getLogger("test.service-stop-quit")

        self.assertTrue(
            handle_service_stop_message(WM_QUIT, shutdown_event, logger)
        )
        self.assertTrue(shutdown_event.is_set())

    def test_unrelated_window_messages_are_ignored(self):
        shutdown_event = threading.Event()
        logger = logging.getLogger("test.service-stop-ignore")

        self.assertFalse(
            handle_service_stop_message(WM_DESTROY, shutdown_event, logger)
        )
        self.assertFalse(shutdown_event.is_set())


if __name__ == "__main__":
    unittest.main()
