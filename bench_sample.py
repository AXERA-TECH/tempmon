#!/usr/bin/env python3
"""Run a benchmark while sampling one board through the tempmon aggregator.

The command is passed after `--`, for example:

    python3 bench_sample.py --board 上海 --json report.json -- bash bench.sh ...

The tool is intentionally read-only with respect to the board: all resource data comes
from GET /api/all on the aggregator.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_API = "http://127.0.0.1:8888/api/all"


def fetch_json(url, timeout=2.0):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def select_board(payload, selector):
    boards = payload.get("boards", []) if isinstance(payload, dict) else []
    wanted = str(selector)
    for board in boards:
        if wanted in (str(board.get("name", "")), str(board.get("ip", ""))):
            return board
    return None


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_sample(board, at=None):
    if not isinstance(board, dict):
        return {"at": at or time.time(), "board_online": False}
    data = board.get("data") if isinstance(board.get("data"), dict) else {}
    memory = data.get("mem") if isinstance(data.get("mem"), dict) else {}
    cmm = data.get("cmm") if isinstance(data.get("cmm"), dict) else {}
    net = data.get("net") if isinstance(data.get("net"), dict) else {}
    inference = board.get("inference") if isinstance(board.get("inference"), dict) else {}
    llama = data.get("llama") if isinstance(data.get("llama"), dict) else {}
    servers = inference.get("servers") if isinstance(inference.get("servers"), list) else []
    temperatures = []
    for zone in data.get("zones", []) if isinstance(data.get("zones"), list) else []:
        if isinstance(zone, dict):
            value = finite(zone.get("c"))
            if value is not None:
                temperatures.append(value)

    total_kb = finite(memory.get("total_kb"))
    available_kb = finite(memory.get("avail_kb"))
    return {
        "at": at if at is not None else time.time(),
        "board_online": bool(board.get("ok")),
        "cmm_used_mb": finite(cmm.get("used_kb")) / 1024 if finite(cmm.get("used_kb")) is not None else None,
        "os_used_mb": (total_kb - available_kb) / 1024
                      if total_kb is not None and available_kb is not None else None,
        "os_available_mb": available_kb / 1024 if available_kb is not None else None,
        "npu_temp_max_c": max(temperatures) if temperatures else None,
        "rx_mbps": finite(net.get("rx_mbps")),
        "tx_mbps": finite(net.get("tx_mbps")),
        "inference_online": int(inference.get("online_count") or 0),
        "inference_processing": sum(
            1 for server in servers
            if isinstance(server, dict) and (
                server.get("state") == "generating" or server.get("slot_state") == "processing"
            )
        ),
        "llama_rss_mb": finite(llama.get("rss_kb")) / 1024 if finite(llama.get("rss_kb")) is not None else None,
    }


def _values(samples, key):
    values = []
    for sample in samples:
        value = finite(sample.get(key))
        if value is not None:
            values.append(value)
    return values


def _metric(samples, key, aggregation, include_delta=False):
    values = _values(samples, key)
    result = {
        "valid": len(values),
        "missing": len(samples) - len(values),
    }
    if not values:
        result.update(value=None, average=None)
        if include_delta:
            result.update(baseline=None, delta=None)
        return result
    value = max(values) if aggregation == "max" else min(values)
    result.update(value=round(value, 4), average=round(statistics.fmean(values), 4))
    if include_delta:
        baseline = statistics.median(values[:max(1, min(5, len(values)))])
        delta = value - baseline
        result.update(baseline=round(baseline, 4), delta=round(delta, 4))
    return result


def summarize_samples(samples):
    return {
        "sample_count": len(samples),
        "board_online_ratio": round(
            sum(1 for sample in samples if sample.get("board_online")) / len(samples), 4
        ) if samples else None,
        "cmm_used_mb": _metric(samples, "cmm_used_mb", "max", include_delta=True),
        "os_used_mb": _metric(samples, "os_used_mb", "max", include_delta=True),
        "os_available_mb": _metric(samples, "os_available_mb", "min", include_delta=True),
        "npu_temp_max_c": _metric(samples, "npu_temp_max_c", "max", include_delta=True),
        "rx_mbps": _metric(samples, "rx_mbps", "max"),
        "tx_mbps": _metric(samples, "tx_mbps", "max"),
        "inference_online_max": max(
            [int(sample.get("inference_online") or 0) for sample in samples] or [0]
        ),
        "inference_processing_max": max(
            [int(sample.get("inference_processing") or 0) for sample in samples] or [0]
        ),
        "llama_rss_mb": _metric(samples, "llama_rss_mb", "max", include_delta=True),
    }


class Sampler:
    def __init__(self, api_url, selector, interval=1.0, fetch=fetch_json):
        self.api_url = api_url
        self.selector = selector
        self.interval = max(0.05, float(interval))
        self.fetch = fetch
        self.samples = []
        self.errors = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def snapshot(self):
        with self._lock:
            return list(self.samples), list(self.errors)

    def clear(self):
        with self._lock:
            self.samples.clear()
            self.errors.clear()

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                board = select_board(self.fetch(self.api_url), self.selector)
                sample = extract_sample(board)
                sample["board_selected"] = board is not None
                with self._lock:
                    self.samples.append(sample)
            except Exception as exc:
                with self._lock:
                    self.errors.append(str(exc))
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.01, self.interval - elapsed))


def _utc_timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def run_benchmark(command, *, api_url, selector, interval=1.0, warmup=3.0,
                  settle=2.0, timeout=None, label="", cwd=None, fetch=fetch_json):
    sampler = Sampler(api_url, selector, interval=interval, fetch=fetch)
    sampler.start()
    deadline = time.monotonic() + max(0.0, warmup)
    while time.monotonic() < deadline:
        samples, _ = sampler.snapshot()
        if samples and warmup <= 0:
            break
        time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))

    baseline_samples, _ = sampler.snapshot()
    if not baseline_samples:
        sampler.stop()
        raise RuntimeError(f"no samples received for board {selector!r}")
    sampler.clear()

    started_at = time.time()
    process = subprocess.Popen(
        command,
        start_new_session=True,
        cwd=cwd,
    )
    try:
        if timeout is None:
            returncode = process.wait()
        else:
            returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        returncode = process.wait()
        timed_out = True
    else:
        timed_out = False
    ended_at = time.time()

    if settle > 0:
        time.sleep(settle)
    sampler.stop()
    run_samples, errors = sampler.snapshot()
    if not run_samples:
        # A very short command can finish between sampler intervals. Preserve at least
        # one post-command reading instead of producing an empty resource report.
        try:
            board = select_board(fetch(api_url), selector)
            run_samples = [extract_sample(board)]
        except Exception as exc:
            errors.append(str(exc))

    return {
        "schema_version": 1,
        "label": label,
        "api_url": api_url,
        "board": selector,
        "command": command,
        "started_at": _utc_timestamp(started_at),
        "ended_at": _utc_timestamp(ended_at),
        "duration_s": round(ended_at - started_at, 3),
        "exit_code": returncode,
        "timed_out": timed_out,
        "baseline": summarize_samples(baseline_samples),
        "benchmark": summarize_samples(run_samples),
        "sampler_errors": errors,
    }


def _flat_report(report):
    benchmark = report.get("benchmark", {})
    baseline = report.get("baseline", {})
    def metric(prefix, name):
        value = benchmark.get(name, {}).get("value")
        base = baseline.get(name, {}).get("value")
        return value, base

    cmm, cmm_base = metric("", "cmm_used_mb")
    os_available, os_available_base = metric("", "os_available_mb")
    os_used, os_used_base = metric("", "os_used_mb")
    temp, temp_base = metric("", "npu_temp_max_c")
    rx, _ = metric("", "rx_mbps")
    tx, _ = metric("", "tx_mbps")
    llama_rss, llama_rss_base = metric("", "llama_rss_mb")
    return {
        "label": report.get("label", ""),
        "board": report.get("board", ""),
        "started_at": report.get("started_at", ""),
        "ended_at": report.get("ended_at", ""),
        "duration_s": report.get("duration_s"),
        "exit_code": report.get("exit_code"),
        "timed_out": str(report.get("timed_out", False)).lower(),
        "sample_count": benchmark.get("sample_count", 0),
        "sampler_errors": len(report.get("sampler_errors", [])),
        "cmm_baseline_mb": cmm_base,
        "cmm_peak_mb": cmm,
        "os_available_baseline_mb": os_available_base,
        "os_available_min_mb": os_available,
        "os_used_baseline_mb": os_used_base,
        "os_used_peak_mb": os_used,
        "npu_temp_baseline_c": temp_base,
        "npu_temp_peak_c": temp,
        "rx_peak_mbps": rx,
        "tx_peak_mbps": tx,
        "inference_online_max": benchmark.get("inference_online_max"),
        "inference_processing_max": benchmark.get("inference_processing_max"),
        "llama_rss_baseline_mb": llama_rss_base,
        "llama_rss_peak_mb": llama_rss,
    }


def write_json(report, path):
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(report, path):
    row = _flat_report(report)
    columns = list(row)
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerow(row)


def write_markdown(report, path):
    row = _flat_report(report)
    title = row.get("label") or "AX8860 benchmark resource report"
    lines = [
        f"# {title}",
        "",
        f"- Board: `{row['board']}`",
        f"- Started: `{row['started_at']}`",
        f"- Duration: `{row['duration_s']} s`",
        f"- Exit code: `{row['exit_code']}`",
        f"- Samples: `{row['sample_count']}`",
        "",
        "| Metric | Baseline | Benchmark |",
        "|---|---:|---:|",
        f"| CMM peak used | {row['cmm_baseline_mb']} MB | {row['cmm_peak_mb']} MB |",
        f"| OS available minimum | {row['os_available_baseline_mb']} MB | {row['os_available_min_mb']} MB |",
        f"| OS used peak | {row['os_used_baseline_mb']} MB | {row['os_used_peak_mb']} MB |",
        f"| NPU max temperature | {row['npu_temp_baseline_c']} °C | {row['npu_temp_peak_c']} °C |",
        f"| llama-server RSS peak | {row['llama_rss_baseline_mb']} MB | {row['llama_rss_peak_mb']} MB |",
        f"| RX peak | — | {row['rx_peak_mbps']} Mbps |",
        f"| TX peak | — | {row['tx_peak_mbps']} Mbps |",
        "",
    ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API, help="aggregator /api/all URL")
    parser.add_argument("--board", required=True, help="board name or IP in boards.json")
    parser.add_argument("--interval", type=float, default=1.0, help="sample interval seconds")
    parser.add_argument("--warmup", type=float, default=3.0, help="baseline sampling seconds")
    parser.add_argument("--settle", type=float, default=2.0, help="post-benchmark sampling seconds")
    parser.add_argument("--timeout", type=float, help="benchmark timeout seconds")
    parser.add_argument("--cwd", help="run the benchmark command in this directory")
    parser.add_argument("--label", default="", help="report/case label")
    parser.add_argument("--json", metavar="PATH", help="write JSON report")
    parser.add_argument("--csv", metavar="PATH", help="write one-row CSV summary")
    parser.add_argument("--markdown", metavar="PATH", help="write Markdown summary")
    parser.add_argument("--no-propagate-exit", action="store_true", help="always exit 0 after writing reports")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required, for example: -- bash bench.sh")

    try:
        report = run_benchmark(
            command,
            api_url=args.api,
            selector=args.board,
            interval=args.interval,
            warmup=args.warmup,
            settle=args.settle,
            timeout=args.timeout,
            label=args.label,
            cwd=args.cwd,
        )
    except Exception as exc:
        print(f"bench_sample: {exc}", file=sys.stderr)
        return 2

    if args.json:
        write_json(report, args.json)
    if args.csv:
        write_csv(report, args.csv)
    if args.markdown:
        write_markdown(report, args.markdown)
    if not args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    summary = _flat_report(report)
    print(
        "bench_sample: exit={exit}, samples={samples}, CMM peak={cmm} MB, "
        "OS avail min={os_avail} MB, NPU max={temp} C".format(
            exit=summary["exit_code"],
            samples=summary["sample_count"],
            cmm=summary["cmm_peak_mb"],
            os_avail=summary["os_available_min_mb"],
            temp=summary["npu_temp_peak_c"],
        ),
        file=sys.stderr,
    )
    return 0 if args.no_propagate_exit else int(report["exit_code"] or 0)


if __name__ == "__main__":
    raise SystemExit(main())
