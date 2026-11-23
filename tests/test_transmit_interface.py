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
        self.assertIn('txity_output_body = create_output_provider_row(', self.source)
        self.assertIn('icecast_output_body = create_output_provider_row(', self.source)
        self.assertIn('label="Move up"', self.source)
        self.assertIn('label="Move down"', self.source)
        self.assertIn('output_order', self.source)
        self.assertIn('"Remote Icecast servers"', self.source)
        self.assertIn('"Audio input"', self.source)
        self.assertIn('def edit_icecast_destination(', self.source)
        self.assertIn('def add_icecast_destination(', self.source)
        self.assertIn('def remove_selected_icecast_destination(', self.source)
        self.assertIn('tx_icecast_bitrate_var', self.source)
        self.assertIn('schedule_live_reconfigure()', self.source)

    def test_audio_input_is_reordered_before_output_card_as_a_sibling(self):
        reorder_calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pack_configure"
        ]
        self.assertTrue(
            any(
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "audio_card"
                and any(
                    keyword.arg == "before"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "icecast_card"
                    for keyword in node.keywords
                )
                for node in reorder_calls
            )
        )
        self.assertFalse(
            any(
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "icecast_card"
                and any(
                    keyword.arg == "before"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "station_card"
                    for keyword in node.keywords
                )
                for node in reorder_calls
            )
        )

    def test_txity_configuration_is_inside_its_collapsible_row(self):
        self.assertIn("station_card = make_card(txity_output_body, padding=16)", self.source)
        self.assertIn("signal_card = make_card(txity_output_body, padding=16)", self.source)
        self.assertIn("advanced_container = ttk.Frame(txity_output_body)", self.source)
        self.assertIn("cw_card = make_card(txity_output_body, padding=16)", self.source)
        self.assertIn("connect_card = make_card(txity_output_body, padding=16)", self.source)
        self.assertIn("audio_card = make_card(page, padding=16)", self.source)

    def test_provider_toggles_are_independent_and_persisted(self):
        self.assertIn("txity_output_enabled_var", self.source)
        self.assertIn("icecast_output_enabled_var", self.source)
        self.assertIn('"txity_output_enabled": txity_output_enabled_var.get()', self.source)
        self.assertIn('"icecast_output_enabled": icecast_output_enabled_var.get()', self.source)
        self.assertIn('txity_enabled=request["txity_enabled"]', self.source)
        self.assertIn('if txity_output_enabled_var.get() and not discord_authenticated and not has_configured_icecast_output():', self.source)
        self.assertIn('def has_configured_icecast_output() -> bool:', self.source)
        self.assertIn('page not in {"discord", "settings", "transmit"}', self.source)

    def test_builtin_icecast_supports_codec_selection(self):
        self.assertIn('tx_icecast_server_format_var', self.source)
        self.assertIn('"audio_format": selected_server_format', self.source)
        self.assertIn('"bitrate_kbps": built_in_server_bitrate', self.source)
        self.assertIn('("ogg_opus", "ogg_vorbis", "aac_plus", "ogg_flac", "mp3")', self.source)
        self.assertIn('"Bitrate (kbps)"', self.source)

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


    def test_station_directory_is_in_the_txity_menu(self):
        self.assertIn('def open_station_directory()', self.source)
        self.assertIn('txity_menu_items.add_command(', self.source)
        self.assertIn('label="Station directory"', self.source)
        self.assertNotIn('("Stations", "stations")', self.source)
        self.assertNotIn('elif current_page == "stations":', self.source)

    def test_scroll_canvas_updates_viewport_width_separately_from_content_region(self):
        start = self.source.index("def create_scrollable_page(")
        end = self.source.index("def show_stations(", start)
        helper = self.source[start:end]
        self.assertIn("if event is not None and event.widget is canvas:", helper)
        self.assertIn("canvas.itemconfigure(window_id, width=width)", helper)
        self.assertIn("bounds != last_scrollregion", helper)
        self.assertNotIn("canvas.itemconfigure(window_id, width=canvas.winfo_width())", helper)

    def test_canvas_scroll_is_applied_without_idle_queue(self):
        self.assertIn('canvas.yview_scroll(units, "units")', self.source)
        self.assertNotIn('root.after_idle(flush_pending_scroll)', self.source)
        self.assertIn('yscrollincrement=SCROLL_UNIT_PIXELS', self.source)

    def test_main_pages_are_prebuilt_and_reused(self):
        self.assertIn('for page_name in ("gate", "transmit", "discord", "settings", "station_detail")', self.source)
        self.assertIn('show_transmit(page_frames["transmit"])', self.source)
        self.assertIn('frame.pack_forget()', self.source)
        self.assertIn('("Accounts", "discord")', self.source)

    def test_transmit_settings_use_application_data_folder(self):
        self.assertIn('return app_data_path("transmit-settings.json")', self.source)


if __name__ == "__main__":
    unittest.main()
