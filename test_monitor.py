#!/usr/bin/env python3
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import re
from pathlib import Path
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("monitor", str(HERE / "monitor.py"))
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class LlamaProbeTests(unittest.TestCase):
    def probe(self, health, models=None, slots=None):
        responses = {
            "/health": health,
            "/v1/models": models or (None, None, "connection refused"),
            "/slots": slots or (None, None, "connection refused"),
        }

        def fake_get_json(url, timeout):
            for path, value in responses.items():
                if url.endswith(path):
                    return value
            raise AssertionError(url)

        with mock.patch.object(monitor, "get_json", side_effect=fake_get_json):
            return monitor.probe_llama_port("192.0.2.2", 18082)

    def test_ready_llama_server(self):
        result = self.probe(
            (200, {"status": "ok"}, None),
            (200, {"data": [{"id": "Qwen3.6-35B-A3B"}]}, None),
            (200, [{"id": 0, "is_processing": False, "n_ctx": 262144, "n_prompt_tokens": 128}], None),
        )
        self.assertTrue(result["is_llama"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "Qwen3.6-35B-A3B")
        self.assertEqual(result["context_total"], 262144)

    def test_generating_llama_server(self):
        result = self.probe(
            (200, {"status": "ok"}, None),
            (200, {"data": [{"id": "model"}]}, None),
            (200, [
                {"is_processing": False, "n_ctx": 1000, "n_prompt_tokens": 100},
                {"is_processing": True, "n_ctx": 2000, "n_prompt_tokens": 200},
            ], None),
        )
        self.assertEqual(result["state"], "generating")
        self.assertEqual(result["processing"], 1)
        self.assertEqual(result["context_used"], 300)
        self.assertEqual(result["context_total"], 3000)

    def test_refused_port_is_not_a_llama_service(self):
        result = self.probe((None, None, "[Errno 111] Connection refused"))
        self.assertFalse(result["is_llama"])
        self.assertEqual(result["state"], "offline")

    def test_non_llama_http_service_is_unknown(self):
        result = self.probe((200, {"hello": "world"}, None))
        self.assertFalse(result["is_llama"])
        self.assertEqual(result["state"], "unknown-service")


class RangeTests(unittest.TestCase):
    def test_default_range(self):
        self.assertEqual(monitor.board_llama_range({}), (18080, 18089))

    def test_custom_range(self):
        self.assertEqual(
            monitor.board_llama_range({"llama_port_min": 18081, "llama_port_max": 18084}),
            (18081, 18084),
        )

    def test_reversed_and_oversized_ranges_are_guarded(self):
        self.assertEqual(
            monitor.board_llama_range({"llama_port_min": 18084, "llama_port_max": 18081}),
            (18081, 18084),
        )
        self.assertEqual(
            monitor.board_llama_range({"llama_port_min": 1, "llama_port_max": 9999}),
            (18080, 18089),
        )

    def test_explicit_port_list_can_include_out_of_range_service(self):
        ports = monitor.board_llama_ports({"llama_ports": ["18080-18089", 18500]})
        self.assertEqual(len(ports), 11)
        self.assertIn(18080, ports)
        self.assertIn(18089, ports)
        self.assertIn(18500, ports)

    def test_explicit_port_list_falls_back_to_default_range(self):
        self.assertEqual(monitor.board_llama_ports({}), set(range(18080, 18090)))


class HistoryTests(unittest.TestCase):
    def test_history_sample_extracts_device_and_inference_values(self):
        result = {
            "ok": True,
            "data": {
                "mem": {"total_kb": 1024, "avail_kb": 256},
                "cmm": {"used_kb": 512},
                "net": {"rx_mbps": "12.5", "tx_mbps": 2.25},
                "npu": {"util_avg": 87.25},
                "ddr": {"used_bw_gbps": "1.25"},
                "zones": [{"c": 41.5}, {"c": 48.25}, {"c": None}],
                "llama": {"rss_kb": 1024},
            },
            "inference": {
                "online_count": 2,
                "servers": [
                    {"state": "ready", "slot_state": "idle"},
                    {"state": "generating", "slot_state": "processing"},
                ],
            },
        }
        sample = monitor.make_history_sample(result)
        self.assertTrue(sample["agent_online"])
        self.assertEqual(sample["os_used_kb"], 768.0)
        self.assertEqual(sample["os_available_kb"], 256.0)
        self.assertEqual(sample["cmm_used_kb"], 512.0)
        self.assertEqual(sample["npu_temp_max_c"], 48.25)
        self.assertEqual(sample["npu_util_avg"], 87.25)
        self.assertEqual(sample["ddr_bw_gbps"], 1.25)
        self.assertEqual(sample["rx_mbps"], 12.5)
        self.assertEqual(sample["tx_mbps"], 2.25)
        self.assertEqual(sample["inference_online"], 2)
        self.assertEqual(sample["inference_processing"], 1)
        self.assertEqual(sample["llama_rss_kb"], 1024.0)

    def test_offline_board_preserves_missing_values_instead_of_inventing_zero(self):
        sample = monitor.make_history_sample({"ok": False, "data": None, "inference": {}})
        self.assertFalse(sample["agent_online"])
        self.assertIsNone(sample["os_used_kb"])
        self.assertIsNone(sample["os_available_kb"])
        self.assertIsNone(sample["cmm_used_kb"])
        self.assertIsNone(sample["npu_temp_max_c"])
        self.assertIsNone(sample["npu_util_avg"])
        self.assertIsNone(sample["ddr_bw_gbps"])
        self.assertEqual(sample["inference_online"], 0)

    def test_fetch_board_sends_configured_bearer_token(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout=None):
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse()

        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = monitor.fetch_board({"ip": "192.0.2.2", "port": 8080, "token": "secret"})
        self.assertTrue(result["ok"])
        self.assertEqual(captured["authorization"], "Bearer secret")

    def test_history_is_a_bounded_ring_buffer(self):
        try:
            monitor.board_history.pop("bounded", None)
            for index in range(5):
                monitor.record_history(
                    "bounded",
                    {"ok": True, "data": {"cmm": {"used_kb": index}}, "inference": {}},
                    limit=3,
                )
            values = monitor.board_history["bounded"]
            self.assertEqual(len(values), 3)
            self.assertEqual([value["cmm_used_kb"] for value in values], [2.0, 3.0, 4.0])
        finally:
            monitor.board_history.pop("bounded", None)

    def test_snapshot_omits_history_by_default_for_fast_polling(self):
        old_cache = monitor.cache
        old_history = monitor.board_history
        try:
            monitor.cache = {
                "bounded": {
                    "name": "bounded",
                    "received_at": 100.0,
                    "last_success_at": 100.0,
                    "agent": {"state": "online"},
                }
            }
            monitor.board_history = {
                "bounded": [{"t": 1.0}, {"t": 2.0}, {"t": 3.0}],
            }
            fast = monitor.get_snapshot()
            self.assertNotIn("history", fast[0])

            explicit = monitor.get_snapshot(include_history=True, history_limit=2)
            self.assertEqual([item["t"] for item in explicit[0]["history"]], [2.0, 3.0])
        finally:
            monitor.cache = old_cache
            monitor.board_history = old_history


class StaleDataTests(unittest.TestCase):
    def test_short_agent_failure_preserves_last_good_sample(self):
        now = 2000.0
        previous = {
            "ok": True,
            "data": {"mem": {"total_kb": 100}},
            "llama": {"online_count": 1},
            "inference": {"online_count": 1},
            "received_at": now - 2,
            "last_success_at": now - 2,
        }
        result = monitor.preserve_recent_good_data(
            {"ok": False, "data": None, "error": "timed out", "agent": {"online": False}},
            previous,
            now=now,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["data"], previous["data"])
        self.assertEqual(result["inference"], previous["inference"])
        self.assertEqual(result["data_age_ms"], 2000)
        self.assertEqual(result["agent"]["state"], "stale")

    def test_long_agent_failure_does_not_preserve_old_sample(self):
        now = 5000.0
        previous = {
            "ok": True,
            "data": {"mem": {"total_kb": 100}},
            "received_at": now - monitor.STALE_DATA_SECONDS - 1,
            "last_success_at": now - monitor.STALE_DATA_SECONDS - 1,
        }
        result = monitor.preserve_recent_good_data(
            {"ok": False, "data": None, "agent": {}},
            previous,
            now=now,
        )
        self.assertFalse(result.get("stale"))
        self.assertIsNone(result.get("data"))

    def test_web_page_uses_in_flight_guard_and_shows_stale_state(self):
        self.assertIn("var updating=false", monitor.HTML)
        self.assertIn("Board Agent Stale", monitor.HTML)
        self.assertIn("NPU Utilization", monitor.HTML)
        self.assertIn("AbortController", monitor.HTML)
        self.assertIn("controller.abort()},8000", monitor.HTML)

    def test_web_page_script_is_valid_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        match = re.search(r"<script>([\s\S]*?)</script>", monitor.HTML)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as directory:
            script = str(Path(directory) / "monitor-page.js")
            Path(script).write_text(match.group(1), encoding="utf-8")
            subprocess.run([node, "--check", script], check=True)


class BoardRegistrationTests(unittest.TestCase):
    def test_registration_updates_poll_address_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            old_conf = monitor.conf
            old_registrations = monitor.registrations
            old_path = monitor.REGISTRATIONS
            try:
                monitor.conf = {"boards": [{"name": "board-a", "ip": "192.0.2.10", "port": 8080}]}
                monitor.registrations = {}
                monitor.REGISTRATIONS = str(Path(directory) / "registrations.json")
                record = monitor.register_board("board-a", 8080, "192.0.2.20")
                self.assertEqual(record["ip"], "192.0.2.20")
                board = monitor.effective_board(monitor.conf["boards"][0])
                self.assertEqual(board["ip"], "192.0.2.20")
                self.assertEqual(board["registration"]["ip"], "192.0.2.20")
                saved = json.loads(Path(monitor.REGISTRATIONS).read_text(encoding="utf-8"))
                self.assertEqual(saved["board-a"]["ip"], "192.0.2.20")
            finally:
                monitor.conf = old_conf
                monitor.registrations = old_registrations
                monitor.REGISTRATIONS = old_path

    def test_registration_rejects_unknown_or_invalid_board(self):
        old_conf = monitor.conf
        old_registrations = monitor.registrations
        try:
            monitor.conf = {"boards": [{"name": "board-a", "ip": "192.0.2.10"}]}
            monitor.registrations = {}
            with self.assertRaises(LookupError):
                monitor.register_board("other", 8080, "192.0.2.20")
            with self.assertRaises(ValueError):
                monitor.register_board("board-a", 70000, "192.0.2.20")
            with self.assertRaises(ValueError):
                monitor.register_board("board-a", 8080, "not-an-ip")
        finally:
            monitor.conf = old_conf
            monitor.registrations = old_registrations

    def test_report_requires_configured_token(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
            token_file.write("test-token\n")
            token_path = token_file.name
        old_conf = monitor.conf
        try:
            monitor.conf = {"report_token_file": token_path}
            self.assertTrue(monitor.report_token_matches("test-token"))
            self.assertFalse(monitor.report_token_matches("wrong"))
            self.assertFalse(monitor.report_token_matches(""))
        finally:
            monitor.conf = old_conf
            os.unlink(token_path)


if __name__ == "__main__":
    unittest.main()
