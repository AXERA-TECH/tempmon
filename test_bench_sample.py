#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bench_sample", str(HERE / "bench_sample.py"))
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


PAYLOAD = {
    "boards": [
        {
            "name": "board-a",
            "ip": "192.0.2.10",
            "ok": True,
            "data": {
                "mem": {"total_kb": 16 * 1024 * 1024, "avail_kb": 10 * 1024 * 1024},
                "cmm": {"used_kb": 14 * 1024},
                "net": {"rx_mbps": 4.5, "tx_mbps": 2.5},
                "zones": [{"c": 45.2}, {"c": 48.7}],
                "llama": {"rss_kb": 1536},
            },
            "inference": {
                "online_count": 1,
                "servers": [{"state": "generating", "slot_state": "processing"}],
            },
        }
    ]
}


class BenchSampleTests(unittest.TestCase):
    def test_selects_board_and_extracts_metrics(self):
        board = bench.select_board(PAYLOAD, "192.0.2.10")
        sample = bench.extract_sample(board, at=100)
        self.assertEqual(sample["at"], 100)
        self.assertTrue(sample["board_online"])
        self.assertEqual(sample["cmm_used_mb"], 14.0)
        self.assertEqual(sample["os_used_mb"], 6 * 1024)
        self.assertEqual(sample["os_available_mb"], 10 * 1024)
        self.assertEqual(sample["npu_temp_max_c"], 48.7)
        self.assertEqual(sample["inference_processing"], 1)
        self.assertEqual(sample["llama_rss_mb"], 1.5)

    def test_summary_reports_peak_and_missing_samples(self):
        samples = [
            {"board_online": True, "cmm_used_mb": 10, "os_available_mb": 8, "npu_temp_max_c": 40},
            {"board_online": True, "cmm_used_mb": None, "os_available_mb": None, "npu_temp_max_c": None},
            {"board_online": False, "cmm_used_mb": 14, "os_available_mb": 6, "npu_temp_max_c": 50},
        ]
        summary = bench.summarize_samples(samples)
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["cmm_used_mb"]["value"], 14)
        self.assertEqual(summary["cmm_used_mb"]["missing"], 1)
        self.assertEqual(summary["os_available_mb"]["value"], 6)
        self.assertEqual(summary["npu_temp_max_c"]["value"], 50)
        self.assertEqual(summary["llama_rss_mb"]["valid"], 0)

    def test_runs_benchmark_with_injected_fetch(self):
        report = bench.run_benchmark(
            [sys.executable, "-c", "print('benchmark-ok')"],
            api_url="memory://api",
            selector="board-a",
            interval=0.03,
            warmup=0.09,
            settle=0.03,
            label="unit-case",
            fetch=lambda url: PAYLOAD,
        )
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["timed_out"])
        self.assertGreaterEqual(report["baseline"]["sample_count"], 1)
        self.assertGreaterEqual(report["benchmark"]["sample_count"], 1)
        self.assertEqual(report["benchmark"]["cmm_used_mb"]["value"], 14.0)
        self.assertEqual(report["benchmark"]["llama_rss_mb"]["value"], 1.5)

    def test_writes_json_csv_and_markdown(self):
        report = bench.run_benchmark(
            [sys.executable, "-c", "pass"],
            api_url="memory://api",
            selector="board-a",
            interval=0.03,
            warmup=0.06,
            settle=0.03,
            label="export-case",
            fetch=lambda url: PAYLOAD,
        )
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "report.json"
            csv_path = Path(directory) / "report.csv"
            markdown_path = Path(directory) / "report.md"
            bench.write_json(report, json_path)
            bench.write_csv(report, csv_path)
            bench.write_markdown(report, markdown_path)

            parsed = json.loads(json_path.read_text())
            self.assertEqual(parsed["label"], "export-case")
            self.assertIn("cmm_peak_mb", csv_path.read_text())
            self.assertIn("# export-case", markdown_path.read_text())


if __name__ == "__main__":
    unittest.main()
