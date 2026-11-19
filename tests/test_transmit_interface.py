import ast
import unittest
from pathlib import Path


MAIN_FILE = Path(__file__).resolve().parents[1] / "main.py"


class TransmitInterfaceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_FILE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_drain_events_declares_frequency_retry_context_nonlocal(self):
        drain_events = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "drain_events"
        )
        nonlocal_names = {
            name
            for node in ast.walk(drain_events)
            if isinstance(node, ast.Nonlocal)
            for name in node.names
        }
        self.assertIn("frequency_retry_context", nonlocal_names)

    def test_drm_metadata_card_is_inside_advanced_settings(self):
        metadata_assignment = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "drm_metadata_card"
                for target in node.targets
            )
        )
        call = metadata_assignment.value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Name)
        self.assertEqual(call.func.id, "make_card")
        self.assertIsInstance(call.args[0], ast.Name)
        self.assertEqual(call.args[0].id, "advanced_container")
        self.assertNotIn("drm_metadata_slot", self.source)


    def test_start_stop_button_stays_in_transmit_header(self):
        assignment = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "tx_transmit_button"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "make_button"
        )
        self.assertIsInstance(assignment.value.args[0], ast.Name)
        self.assertEqual(assignment.value.args[0].id, "hero")


    def test_transmit_status_is_next_to_start_stop_button(self):
        labels = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_label"
        ]
        status_labels = [
            node for node in labels
            if any(
                keyword.arg == "variable"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "tx_status_var"
                for keyword in node.keywords
            )
        ]
        self.assertTrue(status_labels)
        self.assertTrue(
            any(
                node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "hero"
                for node in status_labels
            )
        )
        self.assertNotIn("status_card = make_card(page", self.source)

    def test_live_reconfigure_is_debounced_and_restarts_active_transmission(self):
        self.assertIn("def schedule_live_reconfigure(", self.source)
        self.assertIn("root.after(\n                900,\n                apply_live_reconfigure,", self.source)
        self.assertIn("transmitter.stop()", self.source)
        self.assertIn("start_pending_live_reconfigure()", self.source)
        self.assertIn("def build_transmission_request()", self.source)

    def test_every_transmit_setting_is_watched_for_live_changes(self):
        self.assertIn(
            'setting_variable.trace_add("write", schedule_live_reconfigure)',
            self.source,
        )
        self.assertIn(
            'variable.trace_add("write", schedule_live_reconfigure)',
            self.source,
        )
        self.assertIn(
            'tx_audio_source_var',
            self.source,
        )
        self.assertIn(
            'tx_drm_service_label_var',
            self.source,
        )

    def test_drm_progress_indicator_is_attached_to_signal_card(self):
        assignment = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "drm_progressbar"
                for target in node.targets
            )
        )
        call = assignment.value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "Progressbar")
        self.assertIsInstance(call.func.value, ast.Name)
        self.assertEqual(call.func.value.id, "ttk")
        self.assertIsInstance(call.args[0], ast.Name)
        self.assertEqual(call.args[0].id, "signal_card")

    def test_advanced_signal_controls_use_collapsed_container(self):
        assignments = {}
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value

        for card_name in ("fade_card", "audio_filter_card"):
            with self.subTest(card=card_name):
                call = assignments[card_name]
                self.assertIsInstance(call, ast.Call)
                self.assertEqual(call.func.id, "make_card")
                self.assertEqual(call.args[0].id, "advanced_container")

        self.assertIn('advanced_toggle.pack(anchor="w"', self.source)
        self.assertIn('advanced_container.pack(fill="x", before=audio_card', self.source)


if __name__ == "__main__":
    unittest.main()
