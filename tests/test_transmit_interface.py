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

    def test_icecast_destination_management_is_available_on_transmit_page(self):
        self.assertIn('icecast_card = make_card(page, padding=16)', self.source)
        self.assertIn('"Output destinations"', self.source)
        self.assertIn('"TXITY virtual transmitter"', self.source)
        self.assertIn('"Remote Icecast servers"', self.source)
        self.assertIn('"Audio input"', self.source)
        self.assertIn('def edit_icecast_destination(', self.source)
        self.assertIn('def add_icecast_destination(', self.source)
        self.assertIn('def remove_selected_icecast_destination(', self.source)
        self.assertIn('tx_icecast_bitrate_var', self.source)
        self.assertIn('schedule_live_reconfigure()', self.source)

    def test_embedded_icecast_server_settings_are_saved_and_passed_to_transmitter(self):
        for setting in (
            "tx_icecast_server_enabled_var",
            "tx_icecast_server_port_var",
            "tx_icecast_server_mount_var",
            "tx_icecast_server_password_var",
            "tx_icecast_server_max_listeners_var",
        ):
            self.assertIn(setting, self.source)
        self.assertIn('icecast_server_settings=request["icecast_server_settings"]', self.source)
        self.assertIn('icecast_server_settings', self.source)

    def test_icecast_destination_editor_has_per_output_format_and_bitrate(self):
        self.assertIn('available_icecast_formats()', self.source)
        self.assertIn('"Audio format"', self.source)
        self.assertIn('"Bitrate (kbps)"', self.source)
        self.assertIn('"format": format_labels.get(format_var.get(), "mp3")', self.source)
        self.assertIn('"bitrate": bitrate_var.get()', self.source)

    def test_icecast_connection_events_update_destination_status(self):
        self.assertIn('elif kind == "tx_icecast_state":', self.source)
        self.assertIn('tx_icecast_states[destination_name] = destination_state', self.source)
        self.assertIn('tx_icecast_status_var.set(destination_text)', self.source)

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
        self.assertIn('advanced_container.pack(fill="x", before=cw_card', self.source)


if __name__ == "__main__":
    unittest.main()
