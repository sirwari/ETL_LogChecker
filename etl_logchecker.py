#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils
from collections import defaultdict
from typing import Any, Callable, Iterable, Iterator


class XmlEventWriter:
    def __init__(self, path: str) -> None:
        self._handle = open(path, "w", encoding="utf-8", errors="replace")
        self._handle.write('<?xml version="1.0" encoding="utf-8"?>\n')
        self._handle.write("<Events>\n")
        self._open = True

    def write_event(self, event: dict[str, Any]) -> None:
        if not self._open:
            return

        def write_line(text: str) -> None:
            self._handle.write(text)

        write_line("  <Event>\n")
        for key in (
            "ProviderName",
            "ProviderGuid",
            "EventName",
            "OpcodeName",
            "Timestamp",
            "ProcessID",
        ):
            value = event.get(key)
            if value is None:
                continue
            text = saxutils.escape(str(value))
            write_line(f"    <{key}>{text}</{key}>\n")

        event_data = event.get("EventData")
        if isinstance(event_data, dict) and event_data:
            write_line("    <EventData>\n")
            for data_key in sorted(event_data.keys(), key=lambda k: str(k)):
                value = event_data.get(data_key)
                name = saxutils.escape(str(data_key))
                text = "" if value is None else saxutils.escape(str(value))
                write_line(f'      <Data Name="{name}">{text}</Data>\n')
            write_line("    </EventData>\n")

        write_line("  </Event>\n")

    def close(self) -> None:
        if not self._open:
            return
        self._handle.write("</Events>\n")
        self._handle.close()
        self._open = False


TOOL_VERSION = "0.3.0"

EPOCH_SEC_MIN = 946684800  # 2000-01-01
EPOCH_SEC_MAX = 2524608000  # 2050-01-01

TIME_SCALE_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
}


def _infer_timestamp_scale(value: float) -> float:
    if value >= 1e12:
        seconds = value / 1000.0
        if EPOCH_SEC_MIN <= seconds <= EPOCH_SEC_MAX:
            return 1e-3
        return 1e-6
    if value >= 1e10:
        return 1e-6
    if EPOCH_SEC_MIN <= value <= EPOCH_SEC_MAX:
        return 1.0
    if value >= 1e6:
        return 1e-3
    return 1.0


def _parse_time_scale_arg(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("", "auto"):
        return None
    if text in TIME_SCALE_UNITS:
        return TIME_SCALE_UNITS[text]
    try:
        scale = float(text)
    except Exception as exc:
        raise ValueError("Invalid time-scale value") from exc
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Time scale must be a positive number")
    return scale


def _stringify_guid(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    data1 = getattr(value, "data1", None)
    data2 = getattr(value, "data2", None)
    data3 = getattr(value, "data3", None)
    data4 = getattr(value, "data4", None)
    if data1 is not None and data2 is not None and data3 is not None and data4 is not None:
        try:
            data4_bytes = list(data4)
            if len(data4_bytes) >= 8:
                return (
                    f"{int(data1):08x}-{int(data2):04x}-{int(data3):04x}-"
                    f"{data4_bytes[0]:02x}{data4_bytes[1]:02x}-"
                    f"{''.join(f'{b:02x}' for b in data4_bytes[2:8])}"
                )
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return None


def _read_perf_counter_scale(etl_path: str) -> float | None:
    try:
        from etl.etl import ChunkParser  # type: ignore
        from etl.system import System  # type: ignore
        from etl.wmi import WmiBufferHeader  # type: ignore
    except Exception:
        return None

    try:
        header_size = WmiBufferHeader.sizeof()
        with open(etl_path, "rb") as handle:
            header_bytes = handle.read(header_size)
            if len(header_bytes) < header_size:
                return None
            header = WmiBufferHeader.parse(header_bytes)
            saved_offset = int(header.wnode.saved_offset)
            buffer_size = int(header.wnode.buffer_size)
            payload_size = saved_offset - header_size
            if payload_size <= 0:
                return None
            payload = handle.read(payload_size)
            if len(payload) < payload_size:
                return None
            padding = buffer_size - saved_offset
            if padding > 0:
                padding_bytes = handle.read(padding)
                if len(padding_bytes) < padding:
                    return None

        for event in ChunkParser.parse(payload):
            if getattr(event, "type", None) != "SystemTraceRecord":
                continue
            sys_event = System(event.value)
            try:
                mof = sys_event.get_mof()
            except Exception:
                return None
            source = getattr(mof, "source", None)
            perf_freq = getattr(source, "PerfFreq", None)
            if perf_freq is None:
                return None
            freq = int(perf_freq) >> 32
            if freq <= 0:
                freq = int(perf_freq)
            if freq < 1000 or freq > 1_000_000_000:
                return None
            return 1.0 / float(freq)
    except Exception:
        return None


class TimestampScaler:
    def __init__(self, scale: float | None = None) -> None:
        self._scale: float | None = scale

    def scale(self, value: float) -> float:
        if self._scale is None:
            self._scale = _infer_timestamp_scale(value)
        return value * self._scale

    @property
    def current_scale(self) -> float | None:
        return self._scale


class P2Quantile:
    def __init__(self, p: float) -> None:
        if not 0 < p < 1:
            raise ValueError("p must be between 0 and 1")
        self.p = p
        self._count = 0
        self._initial: list[float] = []
        self._q: list[float] = []
        self._n: list[int] = []
        self._np: list[float] = []
        self._dn = [0.0, p / 2.0, p, (1.0 + p) / 2.0, 1.0]

    def add(self, value: float) -> None:
        if self._count < 5:
            self._initial.append(value)
            self._count += 1
            if self._count == 5:
                self._initial.sort()
                self._q = self._initial[:]
                self._n = [1, 2, 3, 4, 5]
                self._np = [
                    1.0,
                    1.0 + 2.0 * self.p,
                    1.0 + 4.0 * self.p,
                    3.0 + 2.0 * self.p,
                    5.0,
                ]
            return

        self._count += 1
        k = 0
        if value < self._q[0]:
            self._q[0] = value
            k = 0
        elif value >= self._q[4]:
            self._q[4] = value
            k = 3
        else:
            for idx in range(4):
                if self._q[idx] <= value < self._q[idx + 1]:
                    k = idx
                    break

        for idx in range(k + 1, 5):
            self._n[idx] += 1
        for idx in range(5):
            self._np[idx] += self._dn[idx]

        for idx in range(1, 4):
            d = self._np[idx] - self._n[idx]
            if (d >= 1 and self._n[idx + 1] - self._n[idx] > 1) or (
                d <= -1 and self._n[idx - 1] - self._n[idx] < -1
            ):
                step = 1 if d > 0 else -1
                qhat = self._parabolic(idx, step)
                if self._q[idx - 1] < qhat < self._q[idx + 1]:
                    self._q[idx] = qhat
                else:
                    self._q[idx] = self._linear(idx, step)
                self._n[idx] += step

    def _parabolic(self, idx: int, step: int) -> float:
        n0, n1, n2 = self._n[idx - 1], self._n[idx], self._n[idx + 1]
        q0, q1, q2 = self._q[idx - 1], self._q[idx], self._q[idx + 1]
        return q1 + step / (n2 - n0) * (
            (n1 - n0 + step) * (q2 - q1) / (n2 - n1)
            + (n2 - n1 - step) * (q1 - q0) / (n1 - n0)
        )

    def _linear(self, idx: int, step: int) -> float:
        return self._q[idx] + step * (
            (self._q[idx + step] - self._q[idx]) / (self._n[idx + step] - self._n[idx])
        )

    def value(self) -> float | None:
        if self._count == 0:
            return None
        if self._count < 5:
            data = sorted(self._initial)
            pos = int(round((len(data) - 1) * self.p))
            return data[pos]
        return self._q[2]


class QuantileTracker:
    def __init__(self, *percentiles: float) -> None:
        self._trackers = {p: P2Quantile(p) for p in percentiles}

    def add(self, value: float) -> None:
        for tracker in self._trackers.values():
            tracker.add(value)

    def values(self) -> dict[float, float | None]:
        return {p: tracker.value() for p, tracker in self._trackers.items()}


def _html_escape(text: str) -> str:
    return saxutils.escape(text, {"'": "&#39;"})


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 1:
        return f"{value * 1000:.1f} ms"
    if value < 60:
        return f"{value:.2f} s"
    minutes = value / 60.0
    if minutes < 60:
        return f"{minutes:.2f} min"
    hours = minutes / 60.0
    return f"{hours:.2f} h"


def _format_bytes(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _sanitize_display_text(text: Any) -> str:
    value = "" if text is None else str(text)
    value = re.sub(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", value)
    value = value.replace("\r", "\n")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def render_gui_quickstart_text() -> str:
    return "\n".join(
        [
            "ETL LogChecker GUI Quick Start",
            "1. Install dependencies and the virtual environment.",
            "   macOS/Linux: ./install.sh",
            "   Windows PowerShell: .\\install.ps1",
            "2. Launch the GUI.",
            "   macOS/Linux: ./start.sh",
            "   Windows PowerShell: .\\start.ps1",
            "3. In the GUI, choose an ETL trace and leave Auto-create output files enabled.",
            "4. Click Run Analysis to generate report, metrics JSON, timeline, and plots in a timestamped output folder.",
            "5. Use Compare Metrics for saved JSON comparisons and Agentic Diagnose for Ollama-backed review.",
            "Optional: run `python etl_logchecker.py --gui` directly if you do not want to use the helper scripts.",
        ]
    )


def _shorten_chart_label(value: Any, max_length: int = 14) -> str:
    text = str(value).strip()
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return text[: max_length - 3] + "..."


class ETLAnalyzer:
    KERNEL_PROCESS_GUID = "22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716"
    KERNEL_PROCESS_NAME = "Microsoft-Windows-Kernel-Process"
    KERNEL_NETWORK_NAME = "Microsoft-Windows-Kernel-Network"
    OBSERVER_MAX_BYTES = 512 * 1024 * 1024
    PROGRESS_EVERY = 100_000
    TRACERPT_TIMEOUT_S = 30.0

    def __init__(
        self,
        etl_path: str,
        tracerpt_exe: str | None = None,
        use_tracerpt: bool = True,
        debug: bool = False,
        use_etl_observer: bool = True,
        force_etl_observer: bool = False,
        progress_every: int | None = None,
        timestamp_scale: float | None = None,
    ) -> None:
        self.etl_path = etl_path
        self.tracerpt_exe = tracerpt_exe or "tracerpt.exe"
        self.use_tracerpt = use_tracerpt
        self.debug = debug
        self.use_etl_observer = use_etl_observer
        self.force_etl_observer = force_etl_observer
        self.progress_every = (
            self.PROGRESS_EVERY if progress_every is None else progress_every
        )

        self.process_name_by_pid: dict[int, str] = {}
        self.process_start_ts: dict[int, float] = {}
        self.cpu_time_by_pid: dict[int, float] = defaultdict(float)
        self.net_sent_by_pid: dict[int, int] = defaultdict(int)
        self.net_recv_by_pid: dict[int, int] = defaultdict(int)
        self.net_unknown_by_pid: dict[int, int] = defaultdict(int)

        self._providers_seen = {"process": False, "network": False}
        self._timestamp_scale: float | None = timestamp_scale
        self._last_timestamp: float | None = None
        self._network_bin_s: float | None = None
        self._tracerpt_unavailable = False
        self._network_bins: dict[int, dict[str, int]] = defaultdict(
            lambda: {"sent": 0, "recv": 0}
        )

    def enable_network_trends(self, bin_s: float) -> None:
        self._network_bin_s = max(0.001, float(bin_s))
        self._network_bins = defaultdict(lambda: {"sent": 0, "recv": 0})

    def network_trends(self) -> list[dict[str, float]]:
        if self._network_bin_s is None:
            return []
        rows = []
        for idx in sorted(self._network_bins.keys()):
            bucket = self._network_bins[idx]
            rows.append(
                {
                    "time_s": idx * self._network_bin_s,
                    "sent_bytes": float(bucket.get("sent", 0)),
                    "recv_bytes": float(bucket.get("recv", 0)),
                }
            )
        return rows

    def analyze(self, xml_output: str | None = None) -> list[dict[str, Any]]:
        xml_writer = XmlEventWriter(xml_output) if xml_output else None
        try:
            try:
                self._log(f"Parsing ETL with etl-parser: {self.etl_path}")
                self._process_events(
                    self._iter_events_etl_parser(),
                    xml_writer=xml_writer,
                )
            except RuntimeError as exc:
                message = str(exc)
                if message.startswith("etl-parser is not available") or message.startswith(
                    "Unsupported etl-parser API"
                ):
                    if self._etl_observer_allowed():
                        self._log(
                            "etl-parser unavailable; falling back to etl observer "
                            "(loads entire file into memory)."
                        )
                        errors = self._etl_parser_errors()
                        try:
                            self._process_events_etl_observer(xml_writer=xml_writer)
                        except Exception as obs_exc:
                            if obs_exc.__class__.__name__ in errors:
                                raise RuntimeError(
                                    f"ETL parse error: {obs_exc}"
                                ) from obs_exc
                            raise
                    else:
                        size_text = self._format_bytes(self._etl_size_bytes())
                        raise RuntimeError(
                            "etl-parser unavailable and etl observer disabled for "
                            f"{size_text} trace. Install etl-parser or pass "
                            "--force-etl-observer."
                        ) from exc
                else:
                    raise

            missing = self._missing_providers()
            if missing:
                if self.use_tracerpt:
                    self._log(f"Missing providers: {', '.join(sorted(missing))}.")
                    self._log("Falling back to tracerpt.exe for secondary parsing.")
                    self._process_events(
                        self._iter_events_tracerpt(),
                        only_providers=missing,
                        xml_writer=xml_writer,
                    )
                else:
                    self._log(
                        "Missing providers but tracerpt fallback disabled: "
                        + ", ".join(sorted(missing))
                    )

            self._finalize_open_processes()
            return self._build_rows()
        finally:
            if xml_writer:
                xml_writer.close()

    def _missing_providers(self) -> set[str]:
        missing = set()
        if not self._providers_seen["process"]:
            missing.add("process")
        if not self._providers_seen["network"]:
            missing.add("network")
        return missing

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[etl-analyzer] {message}", file=sys.stderr)

    def _etl_size_bytes(self) -> int | None:
        try:
            return os.path.getsize(self.etl_path)
        except OSError:
            return None

    def _format_bytes(self, value: int | None) -> str:
        if value is None:
            return "unknown size"
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

    def _etl_observer_allowed(self) -> bool:
        if not self.use_etl_observer:
            return False
        if self.force_etl_observer:
            return True
        size = self._etl_size_bytes()
        if size is None:
            return True
        if size > self.OBSERVER_MAX_BYTES:
            self._log(
                "Skipping etl observer for large trace "
                f"({self._format_bytes(size)} > "
                f"{self._format_bytes(self.OBSERVER_MAX_BYTES)})."
            )
            return False
        return True

    def _iter_events_etl_parser(self) -> Iterator[Any]:
        etl_module = None
        module_name = None
        try:
            import etl_parser  # type: ignore

            etl_module = etl_parser
            module_name = "etl_parser"
        except Exception:
            etl_module = None

        if etl_module is None:
            try:
                import etl  # type: ignore

                etl_module = etl
                module_name = "etl"
            except Exception as exc:  # pragma: no cover - depends on runtime env
                raise RuntimeError(
                    "etl-parser is not available. Install it with: pip install etl-parser"
                ) from exc

        errors = self._etl_parser_errors()
        try:
            if module_name == "etl":
                yield from self._iter_events_etl_stream()
                return
            for event in self._probe_etl_parser(etl_module):
                yield event
        except Exception as exc:
            if exc.__class__.__name__ in errors:
                raise RuntimeError(f"ETL parse error: {exc}") from exc
            raise

    def _etl_parser_errors(self) -> set[str]:
        error_names = {"BufferOverflow", "InvalidFileFormat", "InvalidEtlFileHeader"}
        for mod_name in (
            "etl_parser.errors",
            "etl_parser.exceptions",
            "etl.error",
            "etl.errors",
            "etl.exceptions",
        ):
            try:
                mod = __import__(mod_name, fromlist=["*"])
            except Exception:
                continue
            for name in dir(mod):
                if name in error_names:
                    error_names.add(name)
        return error_names

    def _probe_etl_parser(self, etl_parser: Any) -> Iterable[Any]:
        candidates: list[tuple[str, Any]] = [
            ("parse", getattr(etl_parser, "parse", None)),
            ("open", getattr(etl_parser, "open", None)),
        ]
        for cls_name in ("ETLParser", "EtlParser", "ETLFile", "EtlFile"):
            if hasattr(etl_parser, cls_name):
                candidates.append((cls_name, getattr(etl_parser, cls_name)))

        last_error: Exception | None = None
        for name, target in candidates:
            if target is None:
                continue
            try:
                if callable(target):
                    obj = target(self.etl_path)
                else:
                    obj = target
                if hasattr(obj, "__enter__") and hasattr(obj, "__exit__"):
                    with obj as ctx:
                        yield from self._iter_from_object(ctx)
                else:
                    yield from self._iter_from_object(obj)
                return
            except Exception as exc:
                last_error = exc
                continue

        raise RuntimeError(
            "Unsupported etl-parser API. Unable to locate an events iterator."
        ) from last_error

    def _iter_events_etl_stream(self) -> Iterator[Any]:
        from etl.etl import ChunkParser  # type: ignore
        from etl.event import Event  # type: ignore
        from etl.perf import PerfInfo  # type: ignore
        from etl.system import System  # type: ignore
        from etl.trace import Trace  # type: ignore
        from etl.wintrace import WinTrace  # type: ignore
        from etl.wmi import WmiBufferHeader  # type: ignore

        actions = {
            "EventRecord": Event,
            "TraceRecord": Trace,
            "SystemTraceRecord": System,
            "PerfInfoTraceRecord": PerfInfo,
            "WinTraceRecord": WinTrace,
        }

        header_size = WmiBufferHeader.sizeof()
        with open(self.etl_path, "rb") as handle:
            while True:
                header_bytes = handle.read(header_size)
                if not header_bytes:
                    break
                if len(header_bytes) < header_size:
                    raise RuntimeError("ETL parse error: truncated WMI header.")
                header = WmiBufferHeader.parse(header_bytes)
                saved_offset = int(header.wnode.saved_offset)
                buffer_size = int(header.wnode.buffer_size)
                payload_size = saved_offset - header_size
                if payload_size < 0:
                    raise RuntimeError("ETL parse error: invalid buffer header size.")
                payload = handle.read(payload_size)
                if len(payload) < payload_size:
                    raise RuntimeError("ETL parse error: truncated buffer payload.")
                padding = buffer_size - saved_offset
                if padding < 0:
                    raise RuntimeError("ETL parse error: invalid buffer padding size.")
                if padding:
                    padding_bytes = handle.read(padding)
                    if len(padding_bytes) < padding:
                        raise RuntimeError("ETL parse error: truncated buffer padding.")
                for event in ChunkParser.parse(payload):
                    action = actions.get(getattr(event, "type", None))
                    if action is None:
                        continue
                    yield action(event.value)

    def _iter_from_object(self, obj: Any) -> Iterable[Any]:
        for attr in ("iter_events", "events", "parse"):
            if hasattr(obj, attr):
                candidate = getattr(obj, attr)
                if callable(candidate):
                    return candidate()
                return candidate
        if hasattr(obj, "__iter__"):
            return obj
        raise RuntimeError("Unable to iterate over etl-parser output.")

    def _iter_events_tracerpt(self) -> Iterator[dict[str, Any]]:
        if not self.use_tracerpt:
            return iter(())
        if self._tracerpt_unavailable:
            return iter(())

        with tempfile.TemporaryDirectory(prefix="etl_tracerpt_") as temp_dir:
            csv_path = os.path.join(temp_dir, "trace.csv")
            xml_path = os.path.join(temp_dir, "trace.xml")

            if self._run_tracerpt("CSV", csv_path):
                yield from self._iter_tracerpt_csv(csv_path)
                return

            if self._run_tracerpt("XML", xml_path):
                yield from self._iter_tracerpt_xml(xml_path)
                return

        self._log("tracerpt.exe did not produce usable CSV or XML output; skipping.")
        return

    def _run_tracerpt(self, fmt: str, output_path: str) -> bool:
        if self._tracerpt_unavailable:
            return False
        cmd = [
            self.tracerpt_exe,
            self.etl_path,
            "-of",
            fmt,
            "-o",
            output_path,
            "-y",
        ]
        self._log(f"Running tracerpt: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=self.TRACERPT_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            self._tracerpt_unavailable = True
            self._log(
                "tracerpt.exe not found on PATH; skipping tracerpt fallback. "
                "Set --tracerpt-exe to a full path if you want it enabled."
            )
            return False
        except subprocess.TimeoutExpired:
            self._tracerpt_unavailable = True
            self._log(
                f"tracerpt timed out after {self.TRACERPT_TIMEOUT_S:.0f}s; "
                "skipping tracerpt fallback."
            )
            return False
        if result.returncode != 0:
            self._log(f"tracerpt failed: {result.stderr.strip()}")
            return False
        return os.path.isfile(output_path) and os.path.getsize(output_path) > 0

    def _iter_tracerpt_csv(self, csv_path: str) -> Iterator[dict[str, Any]]:
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                event = self._normalize_tracerpt_row(row)
                if event:
                    yield event

    def _iter_tracerpt_xml(self, xml_path: str) -> Iterator[dict[str, Any]]:
        for _, elem in ET.iterparse(xml_path, events=("end",)):
            if self._strip_ns(elem.tag) != "Event":
                continue
            event = self._normalize_tracerpt_xml(elem)
            elem.clear()
            if event:
                yield event

    def _normalize_tracerpt_row(self, row: dict[str, str]) -> dict[str, Any]:
        event_data = self._parse_event_data(row.get("EventData") or row.get("Event Data"))
        return {
            "ProviderName": row.get("Provider Name") or row.get("Provider"),
            "ProviderGuid": row.get("Provider Guid") or row.get("Provider GUID"),
            "EventName": row.get("Event Name")
            or row.get("Task Name")
            or row.get("Event"),
            "OpcodeName": row.get("Opcode Name") or row.get("Opcode"),
            "Timestamp": row.get("Time") or row.get("Timestamp"),
            "ProcessID": row.get("Process ID") or row.get("PID"),
            "EventData": event_data,
        }

    def _normalize_tracerpt_xml(self, elem: ET.Element) -> dict[str, Any]:
        provider_name = None
        provider_guid = None
        timestamp = None
        process_id = None
        event_name = None
        opcode_name = None
        event_data: dict[str, Any] = {}

        for child in elem:
            tag = self._strip_ns(child.tag)
            if tag == "System":
                for sys_child in child:
                    sys_tag = self._strip_ns(sys_child.tag)
                    if sys_tag == "Provider":
                        provider_name = sys_child.attrib.get("Name")
                        provider_guid = sys_child.attrib.get("Guid")
                    elif sys_tag == "TimeCreated":
                        timestamp = sys_child.attrib.get("SystemTime")
                    elif sys_tag == "Execution":
                        process_id = sys_child.attrib.get("ProcessID")
                    elif sys_tag == "Task":
                        event_name = sys_child.text
                    elif sys_tag == "Opcode":
                        opcode_name = sys_child.text
            elif tag == "EventData":
                for data in child:
                    if self._strip_ns(data.tag) != "Data":
                        continue
                    key = data.attrib.get("Name") or "Data"
                    event_data[key] = data.text

        return {
            "ProviderName": provider_name,
            "ProviderGuid": provider_guid,
            "EventName": event_name,
            "OpcodeName": opcode_name,
            "Timestamp": timestamp,
            "ProcessID": process_id,
            "EventData": event_data,
        }

    def _strip_ns(self, tag: str) -> str:
        return tag.split("}", 1)[-1]

    def _parse_event_data(self, text: str | None) -> dict[str, Any]:
        if not text:
            return {}
        data: dict[str, Any] = {}
        for chunk in text.replace("\r", "").replace("\n", ";").split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            data[key] = value
        return data

    def _process_events(
        self,
        events: Iterable[Any],
        only_providers: set[str] | None = None,
        xml_writer: XmlEventWriter | None = None,
    ) -> int:
        count = 0
        progress_every = self.progress_every
        should_log_progress = self.debug and bool(progress_every and progress_every > 0)
        for event in events:
            count += 1
            self._process_event(
                event,
                only_providers=only_providers,
                xml_writer=xml_writer,
            )
            if should_log_progress and count % progress_every == 0:
                self._log(f"Processed {count:,} events")
        if self.debug:
            self._log(f"Processed {count:,} events total")
        return count

    def _process_events_etl_observer(
        self, xml_writer: XmlEventWriter | None = None
    ) -> None:
        from etl.etl import IEtlFileObserver, build_from_stream  # type: ignore

        analyzer = self

        class _Observer(IEtlFileObserver):
            def __init__(
                self, owner: ETLAnalyzer, writer: XmlEventWriter | None
            ) -> None:
                self._owner = owner
                self._writer = writer

            def on_system_trace(self, event: Any) -> None:
                self._owner._process_event(event, xml_writer=self._writer)

            def on_perfinfo_trace(self, event: Any) -> None:
                self._owner._process_event(event, xml_writer=self._writer)

            def on_trace_record(self, event: Any) -> None:
                self._owner._process_event(event, xml_writer=self._writer)

            def on_event_record(self, event: Any) -> None:
                self._owner._process_event(event, xml_writer=self._writer)

            def on_win_trace(self, event: Any) -> None:
                self._owner._process_event(event, xml_writer=self._writer)

        self._log("Loading ETL file into memory for etl observer.")
        with open(self.etl_path, "rb") as handle:
            data = handle.read()
        reader = build_from_stream(data)
        reader.parse(_Observer(analyzer, xml_writer))

    def _process_event(
        self,
        event: Any,
        only_providers: set[str] | None = None,
        xml_writer: XmlEventWriter | None = None,
    ) -> None:
        normalized = self._normalize_event(event)
        if xml_writer:
            xml_writer.write_event(normalized)
        provider = self._classify_provider(normalized)
        if provider is None:
            return
        if only_providers and provider not in only_providers:
            return
        if provider == "process":
            self._providers_seen["process"] = True
            self._handle_process_event(normalized)
        elif provider == "network":
            self._providers_seen["network"] = True
            self._handle_network_event(normalized)

    def _classify_provider(self, event: Any) -> str | None:
        provider_guid = self._event_field(
            event, "ProviderGuid", "ProviderGUID", "provider_guid", "providerGuid"
        )
        provider_name = self._event_field(
            event, "ProviderName", "provider_name", "Provider", "provider"
        )
        if provider_guid:
            if str(provider_guid).lower() == self.KERNEL_PROCESS_GUID:
                return "process"
        if provider_name:
            name = str(provider_name)
            if name == self.KERNEL_PROCESS_NAME:
                return "process"
            if name == self.KERNEL_NETWORK_NAME:
                return "network"
        return None

    def _handle_process_event(self, event: Any) -> None:
        event_text = self._event_text(event)
        pid = self._coerce_int(
            self._event_field(event, "ProcessID", "ProcessId", "PID")
        )
        if pid is None:
            pid = self._coerce_int(self._event_field(event, "ProcessId", "ProcessID"))
        if pid is None:
            return

        image_name = self._event_field(
            event, "ImageFileName", "ImageName", "ProcessName"
        )
        if image_name and pid not in self.process_name_by_pid:
            self.process_name_by_pid[pid] = str(image_name)

        timestamp = self._scaled_timestamp(event)
        if timestamp is None:
            return

        if self._is_process_start(event_text):
            if pid in self.process_start_ts:
                self._close_process(pid, timestamp)
            self.process_start_ts[pid] = timestamp
        elif self._is_process_stop(event_text):
            self._close_process(pid, timestamp)

    def _handle_network_event(self, event: Any) -> None:
        pid = self._coerce_int(
            self._event_field(event, "ProcessID", "ProcessId", "PID")
        )
        if pid is None:
            pid = self._coerce_int(self._event_field(event, "ProcessId", "ProcessID"))
        if pid is None:
            return

        event_text = self._event_text(event)
        size = self._coerce_int(
            self._event_field(
                event,
                "Size",
                "TransferSize",
                "NumBytes",
                "Bytes",
                "PayloadSize",
            )
        )
        if size is None:
            return

        timestamp = self._scaled_timestamp(event)

        if "send" in event_text:
            self.net_sent_by_pid[pid] += size
            if self._network_bin_s and timestamp is not None:
                idx = int(timestamp / self._network_bin_s)
                self._network_bins[idx]["sent"] += size
        elif "recv" in event_text or "receive" in event_text:
            self.net_recv_by_pid[pid] += size
            if self._network_bin_s and timestamp is not None:
                idx = int(timestamp / self._network_bin_s)
                self._network_bins[idx]["recv"] += size
        else:
            self.net_unknown_by_pid[pid] += size

    def _event_field(self, event: Any, *names: str) -> Any:
        for name in names:
            value = self._get_value(event, name)
            if value is not None:
                return value
        payload = (
            self._get_value(event, "EventData")
            or self._get_value(event, "payload")
            or self._get_value(event, "Payload")
        )
        if isinstance(payload, dict):
            for name in names:
                for key, value in payload.items():
                    if key.lower() == name.lower():
                        return value
        return None

    def _get_value(self, event: Any, name: str) -> Any:
        if isinstance(event, dict):
            if name in event:
                return event[name]
            for key, value in event.items():
                if key.lower() == name.lower():
                    return value
        if hasattr(event, name):
            return getattr(event, name)
        name_lower = name.lower()
        for attr in dir(event):
            if attr.startswith("_"):
                continue
            if attr.lower() == name_lower:
                return getattr(event, attr)
        return None

    def _payload_to_dict(self, payload: Any) -> dict[str, Any]:
        if payload is None:
            return {}
        if isinstance(payload, dict):
            return payload
        if hasattr(payload, "to_dict") and callable(getattr(payload, "to_dict")):
            try:
                result = payload.to_dict()
                if isinstance(result, dict):
                    return result
            except Exception:
                return {}
        if hasattr(payload, "__dict__"):
            data = getattr(payload, "__dict__", None)
            if isinstance(data, dict):
                return data
        return {}

    def _normalize_event(self, event: Any) -> dict[str, Any]:
        provider_name = self._event_field(
            event, "ProviderName", "provider_name", "Provider", "provider"
        )
        provider_guid = self._event_field(
            event, "ProviderGuid", "ProviderGUID", "provider_guid", "providerGuid"
        )
        event_name = self._event_field(
            event,
            "EventName",
            "event_name",
            "TaskName",
            "task_name",
            "Event",
            "Task",
        )
        opcode_name = self._event_field(
            event, "OpcodeName", "opcode_name", "Opcode", "opcode"
        )
        timestamp = self._event_field(
            event,
            "TimeStamp",
            "Timestamp",
            "TimeCreated",
            "time",
            "Time",
        )
        process_id = self._event_field(event, "ProcessID", "ProcessId", "PID")

        try:
            from etl.event import Event as EtlEvent  # type: ignore
        except Exception:
            EtlEvent = None

        if EtlEvent is not None and isinstance(event, EtlEvent):
            try:
                guid = _stringify_guid(event.source.event_header.provider_id)
                if guid:
                    provider_guid = guid
            except Exception:
                provider_guid = provider_guid
            try:
                timestamp = event.get_timestamp()
            except Exception:
                timestamp = timestamp
            try:
                process_id = event.get_process_id()
            except Exception:
                process_id = process_id

        event_data: dict[str, Any] = {}
        if isinstance(event, dict):
            raw_data = self._get_value(event, "EventData") or self._get_value(
                event, "event_data"
            )
            if isinstance(raw_data, str):
                event_data = self._parse_event_data(raw_data)
            elif isinstance(raw_data, dict):
                event_data = raw_data
        else:
            raw_data = self._get_value(event, "EventData") or self._get_value(
                event, "event_data"
            )
            if isinstance(raw_data, str):
                event_data = self._parse_event_data(raw_data)
            elif isinstance(raw_data, dict):
                event_data = raw_data
            else:
                payload = None
                if hasattr(event, "get_mof") and callable(getattr(event, "get_mof")):
                    try:
                        payload = event.get_mof()
                    except Exception:
                        payload = None
                if payload is None:
                    for method in ("parse_etw", "parse_tracelogging"):
                        if hasattr(event, method) and callable(getattr(event, method)):
                            try:
                                payload = getattr(event, method)()
                                break
                            except Exception:
                                payload = None
                                continue
                event_data = self._payload_to_dict(payload)

        return {
            "ProviderName": provider_name,
            "ProviderGuid": provider_guid,
            "EventName": event_name,
            "OpcodeName": opcode_name,
            "Timestamp": timestamp,
            "ProcessID": process_id,
            "EventData": event_data,
        }

    def _event_text(self, event: Any) -> str:
        parts = []
        for name in (
            "EventName",
            "event_name",
            "TaskName",
            "task_name",
            "OpcodeName",
            "opcode",
            "Opcode",
            "EventType",
        ):
            value = self._event_field(event, name)
            if value:
                parts.append(str(value).lower())
        return " ".join(parts)

    def _is_process_start(self, text: str) -> bool:
        if "processstart" in text or "dcstart" in text:
            return True
        return "start" in text and "stop" not in text and "end" not in text

    def _is_process_stop(self, text: str) -> bool:
        if "processstop" in text or "dcend" in text:
            return True
        return "stop" in text or "end" in text

    def _scaled_timestamp(self, event: Any) -> float | None:
        raw_value = self._event_field(
            event,
            "TimeStamp",
            "Timestamp",
            "TimeCreated",
            "time",
            "Time",
        )
        timestamp = self._parse_timestamp(raw_value)
        if timestamp is None:
            return None
        scaled = self._scale_timestamp(timestamp)
        self._last_timestamp = scaled
        return scaled

    def _parse_timestamp(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dt.datetime):
            return value.timestamp()
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S"):
            try:
                return dt.datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        try:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _scale_timestamp(self, value: float) -> float:
        if self._timestamp_scale is None:
            self._timestamp_scale = _infer_timestamp_scale(value)
        return value * self._timestamp_scale

    def _close_process(self, pid: int, timestamp: float) -> None:
        start = self.process_start_ts.pop(pid, None)
        if start is None:
            return
        delta = timestamp - start
        if delta >= 0:
            self.cpu_time_by_pid[pid] += delta

    def _finalize_open_processes(self) -> None:
        if self._last_timestamp is None:
            return
        for pid, start in list(self.process_start_ts.items()):
            delta = self._last_timestamp - start
            if delta >= 0:
                self.cpu_time_by_pid[pid] += delta
            self.process_start_ts.pop(pid, None)

    def _build_rows(self) -> list[dict[str, Any]]:
        pids = set(self.cpu_time_by_pid.keys())
        pids.update(self.net_sent_by_pid.keys())
        pids.update(self.net_recv_by_pid.keys())
        pids.update(self.net_unknown_by_pid.keys())

        rows: list[dict[str, Any]] = []
        for pid in pids:
            sent = int(self.net_sent_by_pid.get(pid, 0))
            recv = int(self.net_recv_by_pid.get(pid, 0))
            unknown = int(self.net_unknown_by_pid.get(pid, 0))
            rows.append(
                {
                    "pid": pid,
                    "process_name": self.process_name_by_pid.get(pid, "Unknown"),
                    "cpu_time": float(self.cpu_time_by_pid.get(pid, 0.0)),
                    "net_sent": sent,
                    "net_recv": recv,
                    "net_total": sent + recv + unknown,
                }
            )
        return rows


class ETLUXAnalyzer:
    RESPONSE_TIME_SCALE = 1e-7
    HIST_BUCKETS_MS = [0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]

    def __init__(
        self,
        etl_path: str,
        slow_io_ms: int = 50,
        top_n: int = 10,
        debug: bool = False,
        progress_every: int = 200_000,
        timestamp_scale: float | None = None,
        time_scale_source: str = "auto",
        bootlog: bool = False,
        boot_window_s: float = 300.0,
    ) -> None:
        self.etl_path = etl_path
        self.slow_io_ms = slow_io_ms
        self.top_n = top_n
        self.debug = debug
        self.progress_every = progress_every
        self._scaler = TimestampScaler(scale=timestamp_scale)
        self.time_scale_source = time_scale_source
        self.bootlog_enabled = bootlog
        self.boot_window_s = boot_window_s

        self.start_ts: float | None = None
        self.end_ts: float | None = None

        self.pid_info: dict[int, dict[str, Any]] = {}
        self.pid_start_ts: dict[int, float] = {}
        self.pid_end_ts: dict[int, float] = {}
        self.pid_first_image_ts: dict[int, float] = {}
        self.pid_first_io_ts: dict[int, float] = {}

        self.thread_to_pid: dict[int, int] = {}
        self.fileobj_to_name: dict[int, str] = {}

        self.pid_io: dict[int, dict[str, float]] = defaultdict(
            lambda: {
                "bytes": 0.0,
                "ops": 0.0,
                "slow_ops": 0.0,
                "slow_time_s": 0.0,
            }
        )

        self.total_io_ops = 0
        self.total_io_bytes = 0
        self.slow_io_ops = 0
        self.total_slow_io_time_s = 0.0

        self.file_slow_time: dict[str, float] = defaultdict(float)
        self.file_slow_ops: dict[str, int] = defaultdict(int)

        self.quantiles = QuantileTracker(0.5, 0.95, 0.99)
        self.histogram: dict[str, int] = defaultdict(int)
        self.event_count = 0

        self.boot_milestones: dict[str, float | None] = {
            "boot_start_s": 0.0,
            "smss_start_s": None,
            "wininit_start_s": None,
            "winlogon_start_s": None,
            "logonui_start_s": None,
            "explorer_start_s": None,
            "first_user_app_s": None,
        }
        self.boot_order: list[dict[str, Any]] = []
        self._boot_order_seen: set[int] = set()

        self.system_allowlist = {
            "smss.exe",
            "wininit.exe",
            "winlogon.exe",
            "csrss.exe",
            "services.exe",
            "lsass.exe",
            "fontdrvhost.exe",
            "dwm.exe",
            "sihost.exe",
            "taskhostw.exe",
            "svchost.exe",
            "conhost.exe",
            "explorer.exe",
        }

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[etl-ux] {message}", file=sys.stderr)

    def _extract_timestamp(self, event: Any) -> float | None:
        if hasattr(event, "get_timestamp") and callable(getattr(event, "get_timestamp")):
            try:
                return float(event.get_timestamp())
            except Exception:
                return None
        if hasattr(event, "source"):
            source = event.source
            if hasattr(source, "system_header") and hasattr(source.system_header, "system_time"):
                return float(source.system_header.system_time)
            if hasattr(source, "timestamp"):
                return float(source.timestamp)
            if hasattr(source, "header") and hasattr(source.header, "timestamp"):
                return float(source.header.timestamp)
        return None

    def _to_relative(self, timestamp: float | None) -> float | None:
        if timestamp is None:
            return None
        scaled = self._scaler.scale(timestamp)
        if self.start_ts is None:
            self.start_ts = scaled
        self.end_ts = scaled
        return scaled - self.start_ts

    def _update_histogram(self, response_ms: float) -> None:
        for bound in self.HIST_BUCKETS_MS:
            if response_ms <= bound:
                self.histogram[f"<= {bound} ms"] += 1
                return
        self.histogram["> 5000 ms"] += 1

    def _handle_process(self, mof: Any, event_type: int, rel_ts: float | None) -> None:
        pid = self._safe_int(getattr(mof, "get_process_id", lambda: None)())
        if pid is None:
            pid = self._safe_int(getattr(mof.source, "ProcessId", None))
        if pid is None:
            return

        session_id = getattr(mof.source, "SessionId", None)
        parent_id = self._safe_int(getattr(mof, "get_parent_id", lambda: None)())
        if parent_id is None:
            parent_id = self._safe_int(getattr(mof.source, "ParentId", None))

        image = None
        if hasattr(mof, "get_image_file_name"):
            try:
                image = mof.get_image_file_name()
            except Exception:
                image = None
        if image is None:
            image = getattr(mof.source, "ImageFileName", None)
        image = str(image).strip() if image else None

        command_line = None
        if hasattr(mof, "get_command_line"):
            try:
                command_line = mof.get_command_line()
            except Exception:
                command_line = None

        info = self.pid_info.setdefault(pid, {})
        if image:
            info.setdefault("image", image)
        if command_line:
            info.setdefault("command_line", command_line)
        if session_id is not None:
            info.setdefault("session_id", session_id)
        if parent_id is not None:
            info.setdefault("parent_id", parent_id)

        is_start = event_type in (1, 3)
        is_end = event_type in (2, 4, 39)

        if is_start and rel_ts is not None:
            if pid not in self.pid_start_ts:
                self.pid_start_ts[pid] = rel_ts
            image_lower = image.lower() if image else ""
            if image_lower in self.system_allowlist:
                for milestone_key, milestone_image in (
                    ("smss_start_s", "smss.exe"),
                    ("wininit_start_s", "wininit.exe"),
                    ("winlogon_start_s", "winlogon.exe"),
                    ("logonui_start_s", "logonui.exe"),
                    ("explorer_start_s", "explorer.exe"),
                ):
                    if image_lower == milestone_image and self.boot_milestones[milestone_key] is None:
                        self.boot_milestones[milestone_key] = rel_ts
            if (
                session_id is not None
                and int(session_id) > 0
                and image_lower
                and image_lower not in self.system_allowlist
                and self.boot_milestones["first_user_app_s"] is None
            ):
                self.boot_milestones["first_user_app_s"] = rel_ts
            if (
                self.bootlog_enabled
                and rel_ts <= self.boot_window_s
                and pid not in self._boot_order_seen
            ):
                self._boot_order_seen.add(pid)
                self.boot_order.append(
                    {
                        "pid": pid,
                        "image": image or "Unknown",
                        "session_id": session_id,
                        "parent_id": parent_id,
                        "start_s": rel_ts,
                    }
                )

        if is_end and rel_ts is not None:
            self.pid_end_ts[pid] = rel_ts

    def _handle_thread(self, mof: Any, event_type: int) -> None:
        pid = self._safe_int(getattr(mof, "get_process_id", lambda: None)())
        tid = self._safe_int(getattr(mof, "get_thread_id", lambda: None)())
        if pid is None or tid is None:
            return
        if event_type in (1, 3):
            self.thread_to_pid[tid] = pid
        elif event_type in (2, 4):
            self.thread_to_pid.pop(tid, None)

    def _handle_file(self, mof: Any) -> None:
        fileobj = getattr(mof.source, "FileObject", None)
        if fileobj is None:
            return
        try:
            fileobj = int(fileobj)
        except Exception:
            return
        name = None
        if hasattr(mof, "get_file_name"):
            try:
                name = mof.get_file_name()
            except Exception:
                name = None
        if name:
            self.fileobj_to_name[fileobj] = name

    def _handle_image(self, mof: Any, event_type: int, rel_ts: float | None) -> None:
        if rel_ts is None:
            return
        if event_type not in (10, 3):
            return
        pid = self._safe_int(getattr(mof, "get_process_id", lambda: None)())
        if pid is None:
            pid = self._safe_int(getattr(mof.source, "ProcessId", None))
        if pid is None:
            return
        if pid not in self.pid_first_image_ts:
            self.pid_first_image_ts[pid] = rel_ts

    def _handle_io(self, mof: Any, rel_ts: float | None) -> None:
        thread_id = self._safe_int(getattr(mof, "get_issuing_thread_id", lambda: None)())
        if thread_id is None:
            return
        pid = self.thread_to_pid.get(thread_id)
        if pid is None:
            return

        response_raw = getattr(mof.source, "HighResponseTime", None)
        if response_raw is None:
            response_raw = getattr(mof.source, "HighResResponseTime", None)
        response_time_s = None
        if response_raw is not None:
            response_time_s = float(response_raw) * self.RESPONSE_TIME_SCALE
            self.quantiles.add(response_time_s)
            self._update_histogram(response_time_s * 1000.0)

        transfer_size = getattr(mof.source, "TransferSize", None)
        transfer_bytes = float(transfer_size) if transfer_size is not None else 0.0

        if rel_ts is not None and pid not in self.pid_first_io_ts:
            self.pid_first_io_ts[pid] = rel_ts

        self.total_io_ops += 1
        self.total_io_bytes += transfer_bytes

        pid_entry = self.pid_io[pid]
        pid_entry["ops"] += 1.0
        pid_entry["bytes"] += transfer_bytes

        if response_time_s is not None:
            response_ms = response_time_s * 1000.0
            if response_ms >= self.slow_io_ms:
                self.slow_io_ops += 1
                self.total_slow_io_time_s += response_time_s
                pid_entry["slow_ops"] += 1.0
                pid_entry["slow_time_s"] += response_time_s

                fileobj = getattr(mof.source, "FileObject", None)
                if fileobj is not None:
                    try:
                        fileobj_int = int(fileobj)
                    except Exception:
                        fileobj_int = None
                    if fileobj_int is not None:
                        name = self.fileobj_to_name.get(fileobj_int)
                        if name:
                            self.file_slow_time[name] += response_time_s
                            self.file_slow_ops[name] += 1

    def _safe_int(self, value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    def analyze(self) -> dict[str, Any]:
        from etl.event import Event  # type: ignore
        from etl.perf import PerfInfo  # type: ignore
        from etl.system import System  # type: ignore
        from etl.wmi import EventTraceGroup  # type: ignore

        analyzer = ETLAnalyzer(
            self.etl_path,
            use_tracerpt=False,
            debug=self.debug,
        )

        if self.debug:
            self._log("Starting UX analysis (streaming ETL events).")

        count = 0
        for event in analyzer._iter_events_etl_stream():
            count += 1
            rel_ts = self._to_relative(self._extract_timestamp(event))
            if isinstance(event, (System, PerfInfo)):
                try:
                    mof = event.get_mof()
                except Exception:
                    continue
                if isinstance(event, System):
                    group = event.source.system_header.header.group
                else:
                    group = event.source.header.group
                event_type = getattr(mof, "event_type", None)
                if group == EventTraceGroup.EVENT_TRACE_GROUP_PROCESS and event_type is not None:
                    self._handle_process(mof, int(event_type), rel_ts)
                elif group == EventTraceGroup.EVENT_TRACE_GROUP_THREAD and event_type is not None:
                    self._handle_thread(mof, int(event_type))
                elif group == EventTraceGroup.EVENT_TRACE_GROUP_IO:
                    self._handle_io(mof, rel_ts)
                elif group == EventTraceGroup.EVENT_TRACE_GROUP_FILE:
                    self._handle_file(mof)
                elif group == EventTraceGroup.EVENT_TRACE_GROUP_IMAGE and event_type is not None:
                    self._handle_image(mof, int(event_type), rel_ts)
            elif isinstance(event, Event):
                continue

            if self.debug and self.progress_every > 0 and count % self.progress_every == 0:
                self._log(f"Processed {count:,} events (UX analysis)")

        if self.debug:
            self._log(f"Processed {count:,} events total (UX analysis)")
        self.event_count = count

        return self._build_metrics()

    def _build_metrics(self) -> dict[str, Any]:
        trace_duration = 0.0
        if self.start_ts is not None and self.end_ts is not None:
            trace_duration = max(0.0, self.end_ts - self.start_ts)

        launch_latencies: list[dict[str, Any]] = []
        latency_values: list[float] = []
        for pid, start_ts in self.pid_start_ts.items():
            info = self.pid_info.get(pid, {})
            session_id = info.get("session_id")
            if session_id is None or int(session_id) <= 0:
                continue
            first_image = self.pid_first_image_ts.get(pid)
            first_io = self.pid_first_io_ts.get(pid)
            candidates = [t for t in (first_image, first_io) if t is not None]
            if not candidates:
                continue
            first_signal = min(candidates)
            latency = first_signal - start_ts
            if latency < 0:
                continue
            latency_values.append(latency)
            launch_latencies.append(
                {
                    "pid": pid,
                    "image": info.get("image", "Unknown"),
                    "session_id": session_id,
                    "startup_latency_s": latency,
                    "first_signal": "image" if first_image == first_signal else "disk_io",
                }
            )

        launch_latencies.sort(key=lambda r: r["startup_latency_s"], reverse=True)
        launch_top = launch_latencies[: self.top_n]
        latency_values_sorted = sorted(latency_values)
        latency_stats = {
            "count": len(latency_values_sorted),
            "avg_s": (
                sum(latency_values_sorted) / len(latency_values_sorted)
                if latency_values_sorted
                else None
            ),
            "p50_s": latency_values_sorted[int(0.5 * (len(latency_values_sorted) - 1))]
            if latency_values_sorted
            else None,
            "p95_s": latency_values_sorted[int(0.95 * (len(latency_values_sorted) - 1))]
            if latency_values_sorted
            else None,
            "p99_s": latency_values_sorted[int(0.99 * (len(latency_values_sorted) - 1))]
            if latency_values_sorted
            else None,
        }

        process_lifetimes = []
        for pid, start_ts in self.pid_start_ts.items():
            end_ts = self.pid_end_ts.get(pid, trace_duration)
            duration = max(0.0, end_ts - start_ts)
            info = self.pid_info.get(pid, {})
            process_lifetimes.append(
                {
                    "pid": pid,
                    "image": info.get("image", "Unknown"),
                    "session_id": info.get("session_id"),
                    "lifetime_s": duration,
                }
            )
        process_lifetimes.sort(key=lambda r: r["lifetime_s"], reverse=True)
        process_count = len(process_lifetimes)
        user_process_count = sum(
            1
            for pid in self.pid_start_ts
            if (self._safe_int(self.pid_info.get(pid, {}).get("session_id")) or 0) > 0
        )
        latency_stats["coverage_pct"] = (
            _safe_div(float(len(latency_values_sorted)), float(user_process_count))
            if user_process_count > 0
            else 0.0
        )

        io_quantiles = self.quantiles.values()
        io_percentiles = {
            "p50_s": io_quantiles.get(0.5),
            "p95_s": io_quantiles.get(0.95),
            "p99_s": io_quantiles.get(0.99),
        }

        top_by_slow_time = sorted(
            (
                {
                    "pid": pid,
                    "image": self.pid_info.get(pid, {}).get("image", "Unknown"),
                    "slow_time_s": data["slow_time_s"],
                    "slow_ops": int(data["slow_ops"]),
                }
                for pid, data in self.pid_io.items()
            ),
            key=lambda r: r["slow_time_s"],
            reverse=True,
        )[: self.top_n]

        top_by_slow_ops = sorted(
            (
                {
                    "pid": pid,
                    "image": self.pid_info.get(pid, {}).get("image", "Unknown"),
                    "slow_ops": int(data["slow_ops"]),
                    "slow_time_s": data["slow_time_s"],
                }
                for pid, data in self.pid_io.items()
            ),
            key=lambda r: r["slow_ops"],
            reverse=True,
        )[: self.top_n]

        top_by_io_bytes = sorted(
            (
                {
                    "pid": pid,
                    "image": self.pid_info.get(pid, {}).get("image", "Unknown"),
                    "io_bytes": data["bytes"],
                    "io_ops": int(data["ops"]),
                }
                for pid, data in self.pid_io.items()
            ),
            key=lambda r: r["io_bytes"],
            reverse=True,
        )[: self.top_n]

        top_files = sorted(
            (
                {
                    "file": name,
                    "slow_time_s": time_s,
                    "slow_ops": self.file_slow_ops.get(name, 0),
                }
                for name, time_s in self.file_slow_time.items()
            ),
            key=lambda r: r["slow_time_s"],
            reverse=True,
        )[: self.top_n]

        boot_data = dict(self.boot_milestones)
        if self.bootlog_enabled:
            milestone_values = [
                ts for ts in boot_data.values() if isinstance(ts, (int, float))
            ]
            boot_duration = boot_data.get("explorer_start_s")
            if boot_duration is None and milestone_values:
                boot_duration = max(milestone_values)
            boot_data["boot_duration_s"] = boot_duration
            boot_data["boot_order"] = sorted(
                self.boot_order, key=lambda r: r.get("start_s") or 0.0
            )[: self.top_n]
        boot_data["boot_order_count"] = len(boot_data.get("boot_order", []) or [])

        metadata = {
            "etl_path": self.etl_path,
            "generated_at": dt.datetime.utcnow().isoformat() + "Z",
            "tool_version": TOOL_VERSION,
            "slow_io_ms": self.slow_io_ms,
            "timestamp_scale": self._scaler.current_scale,
            "time_scale_source": self.time_scale_source,
        }

        metrics = {
            "metadata": metadata,
            "trace": {
                "start_ts": self.start_ts,
                "end_ts": self.end_ts,
                "duration_s": trace_duration,
                "event_count": self.event_count,
                "events_per_s": _safe_div(float(self.event_count), trace_duration)
                if trace_duration > 0
                else None,
                "events_per_process": _safe_div(float(self.event_count), float(process_count))
                if process_count > 0
                else None,
                "events_per_user_process": _safe_div(
                    float(self.event_count), float(user_process_count)
                )
                if user_process_count > 0
                else None,
                "process_count": process_count,
                "user_process_count": user_process_count,
                "user_process_ratio_pct": _safe_div(
                    float(user_process_count), float(process_count)
                )
                if process_count > 0
                else 0.0,
            },
            "boot": boot_data,
            "launch_latency": {
                "top": launch_top,
                "stats": latency_stats,
            },
            "io": {
                "total_ops": self.total_io_ops,
                "total_bytes": self.total_io_bytes,
                "avg_bytes_per_op": _safe_div(
                    float(self.total_io_bytes), float(self.total_io_ops)
                )
                if self.total_io_ops > 0
                else 0.0,
                "bytes_per_user_process": _safe_div(
                    float(self.total_io_bytes), float(user_process_count)
                )
                if user_process_count > 0
                else None,
                "throughput_bytes_per_s": _safe_div(
                    float(self.total_io_bytes), trace_duration
                )
                if trace_duration > 0
                else None,
                "slow_ops": self.slow_io_ops,
                "slow_ops_pct": _safe_div(float(self.slow_io_ops), float(self.total_io_ops))
                if self.total_io_ops > 0
                else 0.0,
                "slow_ops_per_user_process": _safe_div(
                    float(self.slow_io_ops), float(user_process_count)
                )
                if user_process_count > 0
                else None,
                "slow_time_s": self.total_slow_io_time_s,
                "slow_time_pct": _safe_div(self.total_slow_io_time_s, trace_duration)
                if trace_duration > 0
                else 0.0,
                "slow_time_per_user_process_s": _safe_div(
                    self.total_slow_io_time_s, float(user_process_count)
                )
                if user_process_count > 0
                else None,
                "slow_ops_per_s": _safe_div(float(self.slow_io_ops), trace_duration)
                if trace_duration > 0
                else 0.0,
                "slow_time_avg_ms": _safe_div(
                    self.total_slow_io_time_s * 1000.0,
                    float(self.slow_io_ops),
                )
                if self.slow_io_ops > 0
                else 0.0,
                "percentiles_s": io_percentiles,
                "histogram": dict(self.histogram),
            },
            "top_processes": {
                "by_slow_time": top_by_slow_time,
                "by_slow_ops": top_by_slow_ops,
                "by_io_bytes": top_by_io_bytes,
            },
            "top_files": top_files,
            "process_lifetimes": process_lifetimes[: self.top_n],
        }

        return metrics


def _build_timeline_rows(
    pid_start_ts: dict[int, float],
    pid_end_ts: dict[int, float],
    pid_info: dict[int, dict[str, Any]],
    pid_first_io_ts: dict[int, float],
    pid_first_image_ts: dict[int, float],
    trace_duration: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pid, start_ts in pid_start_ts.items():
        info = pid_info.get(pid, {})
        end_ts = pid_end_ts.get(pid, trace_duration)
        duration = None if end_ts is None else max(0.0, end_ts - start_ts)
        rows.append(
            {
                "pid": pid,
                "image": info.get("image", "Unknown"),
                "session_id": info.get("session_id"),
                "parent_id": info.get("parent_id"),
                "command_line": info.get("command_line"),
                "start_s": start_ts,
                "end_s": end_ts,
                "duration_s": duration,
                "first_io_s": pid_first_io_ts.get(pid),
                "first_image_s": pid_first_image_ts.get(pid),
            }
        )
    rows.sort(key=lambda r: (r["start_s"] is None, r["start_s"]))
    return rows


def _write_timeline_output(
    rows: list[dict[str, Any]],
    output_path: str,
    fmt: str,
) -> None:
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if fmt == "json":
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2)
        return
    if fmt == "csv":
        fieldnames = [
            "pid",
            "image",
            "session_id",
            "parent_id",
            "command_line",
            "start_s",
            "end_s",
            "duration_s",
            "first_io_s",
            "first_image_s",
        ]
        with open(output_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return
    raise ValueError(f"Unsupported timeline format: {fmt}")


def _collect_network_trends(
    etl_path: str,
    bin_s: float,
    timestamp_scale: float | None,
    debug: bool,
    tracerpt_exe: str | None,
    use_tracerpt: bool,
    use_etl_observer: bool,
    force_etl_observer: bool,
) -> list[dict[str, float]]:
    analyzer = ETLAnalyzer(
        etl_path,
        tracerpt_exe=tracerpt_exe,
        use_tracerpt=use_tracerpt,
        debug=debug,
        use_etl_observer=use_etl_observer,
        force_etl_observer=force_etl_observer,
        timestamp_scale=timestamp_scale,
    )
    analyzer.enable_network_trends(bin_s)
    analyzer.analyze()
    return analyzer.network_trends()


def _write_network_throughput_plot(
    trends: list[dict[str, float]],
    plot_dir: str,
) -> str | None:
    os.makedirs(plot_dir, exist_ok=True)
    if not trends:
        return None
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except Exception:
        print(
            "matplotlib is not installed; skipping plot generation.",
            file=sys.stderr,
        )
        return None

    times = [row["time_s"] for row in trends]
    sent = [row["sent_bytes"] for row in trends]
    recv = [row["recv_bytes"] for row in trends]

    plt.figure(figsize=(9, 4.5))
    plt.plot(times, sent, label="Sent bytes")
    plt.plot(times, recv, label="Recv bytes")
    plt.xlabel("Time (s)")
    plt.ylabel("Bytes per bin")
    plt.title("Network throughput")
    plt.legend()
    plt.tight_layout()

    path = os.path.join(plot_dir, "network_throughput.png")
    plt.savefig(path, dpi=160)
    plt.close()
    return path


def _derive_baseline_metrics_path(
    metrics_path: str | None, report_path: str | None
) -> str:
    if metrics_path:
        root, ext = os.path.splitext(metrics_path)
        ext = ext or ".json"
        return f"{root}_baseline{ext}"
    if report_path:
        report_dir = os.path.dirname(report_path) or "."
        return os.path.join(report_dir, "etl_metrics_baseline.json")
    return "etl_metrics_baseline.json"


def _sort_rows(rows: list[dict[str, Any]], sort_key: str) -> list[dict[str, Any]]:
    if sort_key == "pid":
        return sorted(rows, key=lambda r: r["pid"])
    if sort_key == "net":
        return sorted(rows, key=lambda r: r["net_total"], reverse=True)
    return sorted(rows, key=lambda r: r["cpu_time"], reverse=True)


def _format_rows_table(rows: list[dict[str, Any]]) -> str:
    headers = [
        "PID",
        "Process Name",
        "CPU Time (est, s)",
        "Network I/O (sent/recv bytes)",
    ]
    formatted_rows = []
    for row in rows:
        formatted_rows.append(
            [
                str(row["pid"]),
                row["process_name"],
                f"{row['cpu_time']:.3f}",
                f"{row['net_sent']}/{row['net_recv']}",
            ]
        )

    widths = [len(h) for h in headers]
    for row in formatted_rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def render_line(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    lines = [render_line(headers)]
    lines.append("-+-".join("-" * width for width in widths))
    lines.extend(render_line(row) for row in formatted_rows)
    return "\n".join(lines)


def _render_output(
    rows: list[dict[str, Any]],
    output_format: str,
    sort_key: str,
    max_rows: int,
) -> str:
    rows_sorted = _sort_rows(rows, sort_key)[:max_rows]

    if output_format in ("auto", "pandas"):
        try:
            import pandas as pd  # type: ignore

            df = pd.DataFrame(rows_sorted)
            df["CPU Time (est, s)"] = df["cpu_time"].map(lambda v: f"{v:.3f}")
            df["Network I/O (sent/recv bytes)"] = df.apply(
                lambda r: f"{int(r['net_sent'])}/{int(r['net_recv'])}",
                axis=1,
            )
            df = df.rename(
                columns={
                    "pid": "PID",
                    "process_name": "Process Name",
                }
            )
            df = df[["PID", "Process Name", "CPU Time (est, s)", "Network I/O (sent/recv bytes)"]]
            return df.to_string(index=False)
        except Exception:
            if output_format == "pandas":
                raise

    return _format_rows_table(rows_sorted)


def _render_bar_chart(
    items: list[dict[str, Any]],
    value_key: str,
    label_key: str,
    unit: str,
    width: int = 720,
    height: int = 320,
) -> str:
    if not items:
        return '<div class="empty">No data</div>'
    max_value = max(float(item.get(value_key, 0) or 0) for item in items) or 1.0
    bar_count = len(items)
    padding = 40
    chart_width = width - padding * 2
    chart_height = height - padding * 2
    bar_width = chart_width / max(bar_count, 1)
    parts = [f'<svg class="chart-svg" viewBox="0 0 {width} {height}">']
    parts.append(
        "<defs>"
        '<linearGradient id="barGradient" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#2e6bff"/>'
        '<stop offset="100%" stop-color="#5aa1ff"/>'
        "</linearGradient>"
        "</defs>"
    )
    parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="none"/>')
    for idx, item in enumerate(items):
        value = float(item.get(value_key, 0) or 0)
        label = str(item.get(label_key, ""))
        short_label = _shorten_chart_label(label)
        bar_height = (value / max_value) * chart_height
        x = padding + idx * bar_width
        y = padding + (chart_height - bar_height)
        parts.append("<g>")
        parts.append(f"<title>{_html_escape(label)}</title>")
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width * 0.7:.1f}" '
            f'height="{bar_height:.1f}" rx="4" fill="url(#barGradient)"/>'
        )
        parts.append(
            f'<text x="{x + bar_width * 0.35:.1f}" y="{height - 8}" '
            f'text-anchor="middle" class="chart-label">{_html_escape(short_label)}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width * 0.35:.1f}" y="{y - 6:.1f}" '
            f'text-anchor="middle" class="chart-value">{value:.2f}{unit}</text>'
        )
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def _render_timeline(milestones: dict[str, float | None]) -> str:
    points = [
        ("SMSS", milestones.get("smss_start_s")),
        ("Wininit", milestones.get("wininit_start_s")),
        ("Winlogon", milestones.get("winlogon_start_s")),
        ("LogonUI", milestones.get("logonui_start_s")),
        ("Explorer", milestones.get("explorer_start_s")),
        ("First user app", milestones.get("first_user_app_s")),
    ]
    valid = [(label, ts) for label, ts in points if ts is not None]
    if not valid:
        return '<div class="empty">Boot milestones not captured in this trace.</div>'
    palette = [
        "#2e6bff",
        "#24a28a",
        "#ff8a2d",
        "#c056d6",
        "#f05365",
        "#6b7280",
    ]
    max_ts = max(ts for _, ts in valid if ts is not None) or 1.0
    width = 760
    height = 100
    padding = 40
    chart_width = width - padding * 2
    parts = ['<div class="timeline">', f'<svg class="chart-svg" viewBox="0 0 {width} {height}">']
    parts.append(
        f'<line x1="{padding}" y1="{height/2:.1f}" x2="{width - padding}" '
        f'y2="{height/2:.1f}" stroke="#8892a6" stroke-width="2" />'
    )
    for idx, (_, ts) in enumerate(valid):
        x = padding + (float(ts) / max_ts) * chart_width
        color = palette[idx % len(palette)]
        parts.append(
            f'<circle cx="{x:.1f}" cy="{height/2:.1f}" r="6" fill="{color}" />'
        )
    parts.append("</svg>")
    parts.append('<ul class="timeline-list">')
    for idx, (label, ts) in enumerate(valid):
        color = palette[idx % len(palette)]
        parts.append(
            '<li class="timeline-item">'
            f'<span class="timeline-dot" style="background:{color};"></span>'
            f'<span class="timeline-label">{_html_escape(label)}</span>'
            f'<span class="timeline-time">{_format_seconds(ts)}</span>'
            "</li>"
        )
    parts.append("</ul></div>")
    return "".join(parts)


def _render_histogram(hist: dict[str, int]) -> str:
    if not hist:
        return '<div class="empty">No data</div>'
    def _label_key(label: str) -> float:
        if label.startswith(">"):
            return float("inf")
        parts = label.replace("<= ", "").replace(" ms", "")
        try:
            return float(parts)
        except Exception:
            return float("inf")

    labels = sorted(hist.keys(), key=_label_key)
    counts = [hist[label] for label in labels]
    max_count = max(counts) or 1
    width = 720
    height = 220
    padding = 40
    chart_width = width - padding * 2
    chart_height = height - padding * 2
    bar_width = chart_width / max(len(labels), 1)
    parts = [f'<svg class="chart-svg" viewBox="0 0 {width} {height}">']
    for idx, label in enumerate(labels):
        value = hist[label]
        bar_height = (value / max_count) * chart_height
        x = padding + idx * bar_width
        y = padding + (chart_height - bar_height)
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width * 0.7:.1f}" '
            f'height="{bar_height:.1f}" rx="3" fill="#53b689" />'
        )
        parts.append(
            f'<text x="{x + bar_width * 0.35:.1f}" y="{height - 8}" '
            f'text-anchor="middle" class="chart-label">{_html_escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="empty">No data</div>'
    header_html = "".join(f"<th>{_html_escape(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{_html_escape(str(cell))}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{header_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _compare_metrics(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    def get_path(data: dict[str, Any], *keys: str) -> float | None:
        cursor: Any = data
        for key in keys:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(key)
        if cursor is None:
            return None
        try:
            return float(cursor)
        except Exception:
            return None

    comparisons = {
        "duration_s": (get_path(current, "trace", "duration_s"), get_path(baseline, "trace", "duration_s")),
        "slow_io_ops_pct": (
            get_path(current, "io", "slow_ops_pct"),
            get_path(baseline, "io", "slow_ops_pct"),
        ),
        "slow_io_time_s": (
            get_path(current, "io", "slow_time_s"),
            get_path(baseline, "io", "slow_time_s"),
        ),
        "slow_io_pct": (
            get_path(current, "io", "slow_time_pct"),
            get_path(baseline, "io", "slow_time_pct"),
        ),
        "io_p95_s": (
            get_path(current, "io", "percentiles_s", "p95_s"),
            get_path(baseline, "io", "percentiles_s", "p95_s"),
        ),
        "io_p99_s": (
            get_path(current, "io", "percentiles_s", "p99_s"),
            get_path(baseline, "io", "percentiles_s", "p99_s"),
        ),
        "launch_p95_s": (
            get_path(current, "launch_latency", "stats", "p95_s"),
            get_path(baseline, "launch_latency", "stats", "p95_s"),
        ),
        "explorer_start_s": (
            get_path(current, "boot", "explorer_start_s"),
            get_path(baseline, "boot", "explorer_start_s"),
        ),
        "boot_duration_s": (
            get_path(current, "boot", "boot_duration_s"),
            get_path(baseline, "boot", "boot_duration_s"),
        ),
    }

    deltas = {}
    for key, (cur, base) in comparisons.items():
        if cur is None or base is None:
            deltas[key] = {"current": cur, "baseline": base, "delta": None, "pct": None}
            continue
        delta = cur - base
        pct = _safe_div(delta, base) if base else None
        deltas[key] = {"current": cur, "baseline": base, "delta": delta, "pct": pct}
    return deltas


def _render_report(
    metrics: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    plot_paths: dict[str, str] | None = None,
) -> str:
    trace = metrics.get("trace", {}) or {}
    boot = metrics.get("boot", {})
    boot_order = boot.get("boot_order", []) or []
    boot_duration = boot.get("boot_duration_s")
    io = metrics.get("io", {})
    io_percentiles = io.get("percentiles_s", {})
    launch = metrics.get("launch_latency", {}) or {}
    launch_stats = launch.get("stats", {}) or {}
    comparison = _compare_metrics(metrics, baseline) if baseline else None
    plot_paths = plot_paths or {}

    slow_pct = io.get("slow_time_pct", 0.0) * 100.0
    slow_ops_pct = float(io.get("slow_ops_pct", 0.0) or 0.0) * 100.0
    duration_s = trace.get("duration_s", 0.0) or 0.0
    event_count = trace.get("event_count")
    events_per_s = trace.get("events_per_s")
    events_per_process = trace.get("events_per_process")
    events_per_user_process = trace.get("events_per_user_process")
    process_count = trace.get("process_count")
    user_process_count = trace.get("user_process_count")
    user_process_ratio_pct = float(trace.get("user_process_ratio_pct", 0.0) or 0.0) * 100.0
    avg_bytes_per_op = io.get("avg_bytes_per_op")
    bytes_per_user_process = io.get("bytes_per_user_process")
    io_throughput = io.get("throughput_bytes_per_s")
    slow_ops_per_s = io.get("slow_ops_per_s")
    slow_ops_per_user_process = io.get("slow_ops_per_user_process")
    slow_time_avg_ms = io.get("slow_time_avg_ms")
    slow_time_per_user_process_s = io.get("slow_time_per_user_process_s")
    launch_p95 = launch_stats.get("p95_s")
    launch_avg = launch_stats.get("avg_s")
    launch_coverage_pct = float(launch_stats.get("coverage_pct", 0.0) or 0.0) * 100.0
    boot_order_count = boot.get("boot_order_count")

    launch_top = launch.get("top", [])
    slow_top = metrics.get("top_processes", {}).get("by_slow_time", [])
    slow_ops_top = metrics.get("top_processes", {}).get("by_slow_ops", [])
    io_bytes_top = metrics.get("top_processes", {}).get("by_io_bytes", [])
    top_files = metrics.get("top_files", [])

    comparison_rows = []
    if comparison:
        for label, key, formatter in (
            ("Trace duration", "duration_s", _format_seconds),
            ("Slow I/O ops %", "slow_io_ops_pct", lambda v: f"{v * 100:.2f}%"),
            ("Slow I/O time", "slow_io_time_s", _format_seconds),
            ("Slow I/O %", "slow_io_pct", lambda v: f"{v * 100:.2f}%"),
            ("I/O p95", "io_p95_s", _format_seconds),
            ("I/O p99", "io_p99_s", _format_seconds),
            ("Launch p95", "launch_p95_s", _format_seconds),
            ("Explorer start", "explorer_start_s", _format_seconds),
            ("Boot duration", "boot_duration_s", _format_seconds),
        ):
            entry = comparison.get(key, {})
            cur = entry.get("current")
            base = entry.get("baseline")
            delta = entry.get("delta")
            pct = entry.get("pct")
            is_time_metric = (
                "duration" in key
                or "time" in key
                or key.startswith("io_p")
                or key.startswith("launch_")
                or key.endswith("_start_s")
            )
            comparison_rows.append(
                [
                    label,
                    formatter(cur) if cur is not None else "n/a",
                    formatter(base) if base is not None else "n/a",
                    _format_seconds(delta)
                    if is_time_metric
                    else f"{delta * 100:.2f}%"
                    if delta is not None
                    else "n/a",
                    f"{pct * 100:.2f}%" if pct is not None else "n/a",
                ]
            )

    launch_rows = [
        [
            item.get("image", "Unknown"),
            str(item.get("pid", "")),
            str(item.get("session_id", "")),
            _format_seconds(item.get("startup_latency_s")),
            item.get("first_signal", ""),
        ]
        for item in launch_top
    ]

    slow_rows = [
        [
            item.get("image", "Unknown"),
            str(item.get("pid", "")),
            _format_seconds(item.get("slow_time_s")),
            str(item.get("slow_ops", "")),
        ]
        for item in slow_top
    ]

    slow_ops_rows = [
        [
            item.get("image", "Unknown"),
            str(item.get("pid", "")),
            str(item.get("slow_ops", "")),
            _format_seconds(item.get("slow_time_s")),
        ]
        for item in slow_ops_top
    ]

    io_bytes_rows = [
        [
            item.get("image", "Unknown"),
            str(item.get("pid", "")),
            _format_bytes(item.get("io_bytes")),
            str(item.get("io_ops", "")),
        ]
        for item in io_bytes_top
    ]
    io_bytes_chart = [
        {
            "image": item.get("image", "Unknown"),
            "io_bytes_mb": float(item.get("io_bytes", 0.0) or 0.0) / (1024 * 1024),
        }
        for item in io_bytes_top
    ]

    file_rows = [
        [
            item.get("file", ""),
            _format_seconds(item.get("slow_time_s")),
            str(item.get("slow_ops", "")),
        ]
        for item in top_files
    ]

    io_percentile_rows = [
        ["p50", _format_seconds(io_percentiles.get("p50_s"))],
        ["p95", _format_seconds(io_percentiles.get("p95_s"))],
        ["p99", _format_seconds(io_percentiles.get("p99_s"))],
    ]

    boot_summary_parts = []
    if boot_duration is not None:
        boot_summary_parts.append(f"Boot duration: {_format_seconds(boot_duration)}")
    if boot_order_count is not None:
        boot_summary_parts.append(f"Boot order entries: {int(boot_order_count)}")
    boot_summary = (
        f'<div class="subtitle">{" • ".join(boot_summary_parts)}</div>'
        if boot_summary_parts
        else ""
    )
    boot_order_rows = [
        [
            item.get("image", "Unknown"),
            str(item.get("pid", "")),
            str(item.get("session_id", "")),
            _format_seconds(item.get("start_s")),
        ]
        for item in boot_order
    ]
    boot_order_block = ""
    if boot_order_rows:
        boot_order_block = (
            '<div class="subtitle">Boot order (first '
            + str(len(boot_order_rows))
            + ")</div>"
            + _render_table(
                ["Image", "PID", "Session", "Start"], boot_order_rows
            )
        )

    plot_cards = []
    if plot_paths.get("network_throughput"):
        plot_cards.append(
            '<div class="plot-card"><div class="plot-title">Network throughput</div>'
            f'<img class="plot-image" src="{_html_escape(plot_paths["network_throughput"])}" '
            'alt="Network throughput" /></div>'
        )
    plots_html = ""
    if plot_cards:
        plots_html = (
            '<div class="section"><h2>Trends</h2>'
            '<div class="plot-grid">'
            + "".join(plot_cards)
            + "</div></div>"
        )

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>ETL UX/Performance Report</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6fb;
      --card: #ffffff;
      --text: #141b2d;
      --muted: #5c677d;
      --accent: #2e6bff;
      --accent-soft: #e7efff;
    }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      background: radial-gradient(circle at top, #ffffff, var(--bg));
      color: var(--text);
    }}
    .wrap {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 24px 80px;
    }}
    h1 {{
      font-size: 28px;
      margin-bottom: 8px;
    }}
    h2 {{
      margin-top: 32px;
      font-size: 20px;
    }}
    .subtitle {{
      color: var(--muted);
      margin-bottom: 24px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--card);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 8px 20px rgba(20, 27, 45, 0.08);
    }}
    .card h3 {{
      margin: 0 0 8px;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .card .value {{
      font-size: 24px;
      font-weight: 600;
    }}
    .section {{
      background: var(--card);
      border-radius: 18px;
      padding: 20px;
      margin-top: 20px;
      box-shadow: 0 10px 26px rgba(20, 27, 45, 0.07);
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid #e3e7f0;
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: 0.06em;
    }}
    .empty {{
      color: var(--muted);
      padding: 8px 0;
    }}
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
    }}
    .timeline {{
      display: grid;
      gap: 12px;
    }}
    .timeline-list {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    .timeline-item {{
      display: grid;
      grid-template-columns: 12px 1fr auto;
      align-items: center;
      column-gap: 8px;
      font-size: 12px;
    }}
    .timeline-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
    }}
    .timeline-label {{
      font-weight: 600;
    }}
    .timeline-time {{
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}
    .plot-grid {{
      display: grid;
      gap: 16px;
    }}
    .plot-card {{
      background: #f7f9ff;
      border-radius: 16px;
      padding: 16px;
      border: 1px solid #e3e7f0;
    }}
    .plot-title {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 12px;
    }}
    .plot-image {{
      width: 100%;
      height: auto;
      display: block;
      border-radius: 12px;
      background: #ffffff;
      object-fit: contain;
    }}
    .chart-svg {{
      width: 100%;
      height: auto;
      display: block;
      overflow: visible;
    }}
    .chart-label {{
      font-size: 10px;
      fill: var(--muted);
    }}
    .chart-value {{
      font-size: 10px;
      fill: var(--text);
    }}
    .metric-inline {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 20px;
      margin: 8px 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .metric-inline strong {{
      color: var(--text);
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>ETL UX & Performance Report</h1>
    <div class="subtitle">Generated {_html_escape(metrics.get("metadata", {}).get("generated_at", ""))} • {_html_escape(metrics.get("metadata", {}).get("etl_path", ""))}</div>

    <div class="cards">
      <div class="card"><h3>Trace duration</h3><div class="value">{_format_seconds(duration_s)}</div></div>
      <div class="card"><h3>Event count</h3><div class="value">{int(event_count) if event_count is not None else "n/a"}</div></div>
      <div class="card"><h3>Events / s</h3><div class="value">{f"{events_per_s:.2f}" if events_per_s is not None else "n/a"}</div></div>
      <div class="card"><h3>Events / process</h3><div class="value">{f"{float(events_per_process):.2f}" if events_per_process is not None else "n/a"}</div></div>
      <div class="card"><h3>Events / user process</h3><div class="value">{f"{float(events_per_user_process):.2f}" if events_per_user_process is not None else "n/a"}</div></div>
      <div class="card"><h3>Processes</h3><div class="value">{int(process_count) if process_count is not None else "n/a"}</div></div>
      <div class="card"><h3>User processes</h3><div class="value">{int(user_process_count) if user_process_count is not None else "n/a"}</div></div>
      <div class="card"><h3>User process ratio</h3><div class="value">{user_process_ratio_pct:.2f}%</div></div>
      <div class="card"><h3>Slow I/O time</h3><div class="value">{_format_seconds(io.get("slow_time_s"))}</div></div>
      <div class="card"><h3>Slow I/O %</h3><div class="value">{slow_pct:.2f}%</div></div>
      <div class="card"><h3>Slow ops %</h3><div class="value">{slow_ops_pct:.2f}%</div></div>
      <div class="card"><h3>Slow ops / s</h3><div class="value">{f"{float(slow_ops_per_s):.2f}" if slow_ops_per_s is not None else "n/a"}</div></div>
      <div class="card"><h3>Slow ops / user process</h3><div class="value">{f"{float(slow_ops_per_user_process):.2f}" if slow_ops_per_user_process is not None else "n/a"}</div></div>
      <div class="card"><h3>Avg slow I/O (ms)</h3><div class="value">{f"{float(slow_time_avg_ms):.2f}" if slow_time_avg_ms is not None else "n/a"}</div></div>
      <div class="card"><h3>Slow I/O s / user process</h3><div class="value">{_format_seconds(slow_time_per_user_process_s)}</div></div>
      <div class="card"><h3>I/O p95</h3><div class="value">{_format_seconds(io_percentiles.get("p95_s"))}</div></div>
      <div class="card"><h3>I/O p99</h3><div class="value">{_format_seconds(io_percentiles.get("p99_s"))}</div></div>
      <div class="card"><h3>I/O throughput</h3><div class="value">{_format_bytes(io_throughput) + "/s" if io_throughput is not None else "n/a"}</div></div>
      <div class="card"><h3>Launch p95</h3><div class="value">{_format_seconds(launch_p95)}</div></div>
    </div>

    {f'<div class="section"><h2>Comparison to baseline</h2>{_render_table(["Metric", "Current", "Baseline", "Delta", "Delta %"], comparison_rows)}</div>' if comparison else ''}

    {plots_html}

    <div class="section">
      <h2>Boot timeline</h2>
      {boot_summary}
      {_render_timeline(boot)}
      {boot_order_block}
    </div>

    <div class="section">
      <h2>App launch latency (proxy)</h2>
      <div class="metric-inline">
        <span><strong>Launch avg:</strong> {_format_seconds(launch_avg)}</span>
        <span><strong>Launch p50:</strong> {_format_seconds(launch_stats.get("p50_s"))}</span>
        <span><strong>Launch p95:</strong> {_format_seconds(launch_p95)}</span>
        <span><strong>Launch coverage:</strong> {launch_coverage_pct:.2f}%</span>
        <span><strong>Tracked processes:</strong> {int(process_count) if process_count is not None else "n/a"}</span>
        <span><strong>User processes:</strong> {int(user_process_count) if user_process_count is not None else "n/a"}</span>
        <span><strong>Avg I/O bytes / op:</strong> {_format_bytes(avg_bytes_per_op)}</span>
        <span><strong>I/O bytes / user process:</strong> {_format_bytes(bytes_per_user_process)}</span>
        <span><strong>I/O throughput:</strong> {_format_bytes(io_throughput) + "/s" if io_throughput is not None else "n/a"}</span>
      </div>
      {_render_bar_chart(launch_top, "startup_latency_s", "image", "s")}
      {_render_table(["Image", "PID", "Session", "Latency", "First signal"], launch_rows)}
      <div class="subtitle">Latency is derived from first disk I/O or image load after process start.</div>
    </div>

    <div class="section grid-2">
      <div>
        <h2>Slow I/O offenders (time)</h2>
        {_render_bar_chart(slow_top, "slow_time_s", "image", "s")}
        {_render_table(["Image", "PID", "Slow time", "Slow ops"], slow_rows)}
      </div>
      <div>
        <h2>Slow I/O offenders (count)</h2>
        {_render_bar_chart(slow_ops_top, "slow_ops", "image", "")}
        {_render_table(["Image", "PID", "Slow ops", "Slow time"], slow_ops_rows)}
      </div>
    </div>

    <div class="section grid-2">
      <div>
        <h2>Disk I/O latency</h2>
        {_render_table(["Percentile", "Latency"], io_percentile_rows)}
        {_render_histogram(io.get("histogram", {}))}
      </div>
      <div>
        <h2>Top I/O bytes</h2>
        {_render_bar_chart(io_bytes_chart, "io_bytes_mb", "image", " MB")}
        {_render_table(["Image", "PID", "I/O bytes", "I/O ops"], io_bytes_rows)}
      </div>
    </div>

    <div class="section">
      <h2>Top files by slow I/O time</h2>
      {_render_table(["File", "Slow time", "Slow ops"], file_rows)}
    </div>
  </div>
</body>
</html>
"""
    return html.strip()


def _write_json_file(path: str, data: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def _resolve_time_scale(etl_path: str, time_scale: str | None) -> tuple[float | None, str]:
    override_scale = _parse_time_scale_arg(time_scale or "auto")
    if override_scale is not None:
        return override_scale, "override"
    perf_scale = _read_perf_counter_scale(etl_path)
    if perf_scale is not None:
        return perf_scale, "perf_freq"
    return None, "auto"


def run_analysis_job(
    etl_path: str,
    report_path: str | None = None,
    metrics_output_path: str | None = None,
    compare_metrics_path: str | None = None,
    compare_etl_path: str | None = None,
    timeline_output_path: str | None = None,
    timeline_format: str = "json",
    plot_dir: str | None = None,
    plot_bin_s: float = 1.0,
    slow_io_ms: int = 50,
    top_n: int = 10,
    time_scale: str | None = "auto",
    bootlog: bool = False,
    boot_window_s: float = 300.0,
    debug: bool = False,
    progress_every: int = 200000,
    tracerpt_exe: str | None = None,
    use_tracerpt: bool = True,
    use_etl_observer: bool = True,
    force_etl_observer: bool = False,
    return_metrics: bool = True,
    return_baseline_metrics: bool = False,
    progress_callback: Callable[[str], None] | None = None,
    progress_event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def _report_progress(status_text: str) -> None:
        if progress_callback:
            progress_callback(status_text)

    def _publish_progress(
        status_text: str,
        updates: dict[str, Any] | None = None,
    ) -> None:
        if progress_event_callback:
            progress_event_callback(
                {
                    "status_text": status_text,
                    "updates": updates or {},
                }
            )

    if compare_metrics_path and compare_etl_path:
        raise ValueError("Provide either compare_metrics_path or compare_etl_path, not both.")
    if not os.path.isfile(etl_path):
        raise ValueError(f"ETL file not found: {etl_path}")
    if compare_etl_path and not os.path.isfile(compare_etl_path):
        raise ValueError(f"Baseline ETL file not found: {compare_etl_path}")
    if compare_metrics_path and not os.path.isfile(compare_metrics_path):
        raise ValueError(f"Baseline metrics file not found: {compare_metrics_path}")
    if plot_bin_s <= 0:
        raise ValueError("plot_bin_s must be greater than 0.")
    if boot_window_s <= 0:
        raise ValueError("boot_window_s must be greater than 0.")
    if not use_etl_observer and force_etl_observer:
        raise ValueError(
            "Cannot use force_etl_observer when use_etl_observer is disabled."
        )

    timestamp_scale, time_scale_source = _resolve_time_scale(etl_path, time_scale)

    metrics: dict[str, Any] = {}
    baseline_metrics = None
    baseline_metrics_path = None
    comparison = None
    timeline_rows = None
    timeline_path = None
    warnings: list[str] = []
    plot_paths: dict[str, str] = {}
    report_html = None
    written_metrics_path = None
    written_report_path = None

    ux = ETLUXAnalyzer(
        etl_path,
        slow_io_ms=slow_io_ms,
        top_n=top_n,
        debug=debug,
        progress_every=progress_every,
        timestamp_scale=timestamp_scale,
        time_scale_source=time_scale_source,
        bootlog=bootlog,
        boot_window_s=boot_window_s,
    )
    _report_progress("Analyzing primary ETL trace...")
    _publish_progress("Analyzing primary ETL trace...")
    metrics = ux.analyze()
    _report_progress("Primary ETL analysis complete.")
    _publish_progress("Primary ETL analysis complete.", {"metrics": metrics})

    if metrics_output_path:
        _report_progress("Writing primary metrics JSON...")
        _write_json_file(metrics_output_path, metrics)
        written_metrics_path = metrics_output_path
        _publish_progress(
            "Writing primary metrics JSON...",
            {"metrics_path": written_metrics_path},
        )

    if compare_metrics_path:
        _report_progress("Loading baseline metrics JSON...")
        with open(compare_metrics_path, "r", encoding="utf-8") as handle:
            baseline_metrics = json.load(handle)
        comparison = _compare_metrics(metrics, baseline_metrics)
        _publish_progress(
            "Loading baseline metrics JSON...",
            {
                "baseline_metrics": baseline_metrics,
                "comparison": comparison,
            },
        )
    elif compare_etl_path:
        _report_progress("Analyzing baseline ETL trace...")
        _publish_progress("Analyzing baseline ETL trace...")
        baseline_ux = ETLUXAnalyzer(
            compare_etl_path,
            slow_io_ms=slow_io_ms,
            top_n=top_n,
            debug=debug,
            progress_every=progress_every,
            timestamp_scale=timestamp_scale,
            time_scale_source=time_scale_source,
            bootlog=bootlog,
            boot_window_s=boot_window_s,
        )
        baseline_metrics = baseline_ux.analyze()
        comparison = _compare_metrics(metrics, baseline_metrics)
        _report_progress("Baseline ETL analysis complete.")
        _publish_progress(
            "Baseline ETL analysis complete.",
            {
                "baseline_metrics": baseline_metrics,
                "comparison": comparison,
            },
        )
        if metrics_output_path or report_path:
            _report_progress("Writing baseline metrics JSON...")
            baseline_metrics_path = _derive_baseline_metrics_path(
                metrics_output_path, report_path
            )
            _write_json_file(baseline_metrics_path, baseline_metrics)
            _publish_progress(
                "Writing baseline metrics JSON...",
                {"baseline_metrics_path": baseline_metrics_path},
            )

    if timeline_output_path:
        _report_progress("Writing timeline output...")
        trace_duration = metrics.get("trace", {}).get("duration_s") or 0.0
        timeline_rows = _build_timeline_rows(
            ux.pid_start_ts,
            ux.pid_end_ts,
            ux.pid_info,
            ux.pid_first_io_ts,
            ux.pid_first_image_ts,
            trace_duration,
        )
        _write_timeline_output(timeline_rows, timeline_output_path, timeline_format)
        timeline_path = timeline_output_path
        _publish_progress(
            "Writing timeline output...",
            {"timeline_path": timeline_path},
        )

    if plot_dir:
        os.makedirs(plot_dir, exist_ok=True)
        trends: list[dict[str, float]] | None = None
        _report_progress("Collecting network trends for plots...")
        _publish_progress("Collecting network trends for plots...")
        try:
            trends = _collect_network_trends(
                etl_path,
                plot_bin_s,
                timestamp_scale,
                debug,
                tracerpt_exe,
                use_tracerpt,
                use_etl_observer,
                force_etl_observer,
            )
        except Exception as exc:
            warnings.append(
                f"Plot generation skipped (network trend collection failed: {exc})."
            )
            _publish_progress(
                "Collecting network trends for plots...",
                {"warnings": warnings},
            )

        if trends is not None:
            _report_progress("Rendering network throughput plot...")
            _publish_progress("Rendering network throughput plot...")
            render_failed = False
            try:
                plot_path = _write_network_throughput_plot(trends, plot_dir)
            except Exception as exc:
                render_failed = True
                warnings.append(f"Plot generation skipped ({exc}).")
                _publish_progress(
                    "Rendering network throughput plot...",
                    {"warnings": warnings},
                )
                plot_path = None
            if plot_path:
                if report_path:
                    report_dir = os.path.dirname(report_path) or "."
                    plot_paths["network_throughput"] = os.path.relpath(
                        plot_path, report_dir
                    )
                else:
                    plot_paths["network_throughput"] = plot_path
                _publish_progress(
                    "Rendering network throughput plot...",
                    {"plot_paths": plot_paths},
                )
            elif not render_failed:
                warnings.append("Plot generation skipped (missing matplotlib or no data).")
                _publish_progress(
                    "Rendering network throughput plot...",
                    {"warnings": warnings},
                )

    if report_path:
        _report_progress("Rendering HTML report...")
        _publish_progress("Rendering HTML report...")
        report_html = _render_report(metrics, baseline_metrics, plot_paths)
        report_dir = os.path.dirname(report_path)
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(report_html)
        written_report_path = report_path
        _publish_progress(
            "Rendering HTML report...",
            {
                "report_html": report_html,
                "report_path": written_report_path,
            },
        )

    response: dict[str, Any] = {
        "comparison": comparison,
        "metrics_path": written_metrics_path,
        "baseline_metrics_path": baseline_metrics_path,
        "report_path": written_report_path,
        "report_html": report_html,
        "timeline_path": timeline_path,
        "timeline_rows": timeline_rows,
        "plot_paths": plot_paths,
        "warnings": warnings,
    }

    if return_metrics:
        response["metrics"] = metrics
    if return_baseline_metrics:
        response["baseline_metrics"] = baseline_metrics

    _report_progress("Finalizing results...")
    _publish_progress("Finalizing results...")
    return response


def _suggest_output_paths(etl_path: str | None) -> dict[str, str]:
    if not etl_path:
        return {
            "report_path": "",
            "metrics_output_path": "",
            "timeline_output_path": "",
            "plot_dir": "",
        }
    normalized = os.path.abspath(etl_path)
    directory = os.path.dirname(normalized) or "."
    stem = os.path.splitext(os.path.basename(normalized))[0] or "etl_trace"
    run_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    result_dir = os.path.join(directory, f"{stem}_{run_stamp}")
    return {
        "report_path": os.path.join(result_dir, f"{stem}_report.html"),
        "metrics_output_path": os.path.join(result_dir, f"{stem}_metrics.json"),
        "timeline_output_path": os.path.join(result_dir, f"{stem}_timeline.json"),
        "plot_dir": os.path.join(result_dir, f"{stem}_plots"),
    }


def _read_text_file(path: str | None) -> str | None:
    if not path:
        return None
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _format_json_block(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _format_html_block(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("><", ">\n<")


def _merge_analysis_progress_result(
    current: dict[str, Any] | None,
    updates: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(current or {})
    if not updates:
        return merged
    for key, value in updates.items():
        if key == "plot_paths":
            merged[key] = dict(value or {})
        elif key == "warnings":
            merged[key] = list(value or [])
        else:
            merged[key] = value
    return merged


def _resolve_analysis_output_paths(
    etl_path: str | None,
    auto_generate: bool,
    report_path: str | None = None,
    metrics_output_path: str | None = None,
    timeline_output_path: str | None = None,
    plot_dir: str | None = None,
) -> dict[str, str | None]:
    if auto_generate:
        suggested = _suggest_output_paths(etl_path)
        return {
            "report_path": suggested.get("report_path") or None,
            "metrics_output_path": suggested.get("metrics_output_path") or None,
            "timeline_output_path": suggested.get("timeline_output_path") or None,
            "plot_dir": suggested.get("plot_dir") or None,
        }
    return {
        "report_path": report_path or None,
        "metrics_output_path": metrics_output_path or None,
        "timeline_output_path": timeline_output_path or None,
        "plot_dir": plot_dir or None,
    }


def _build_analysis_summary(
    result: dict[str, Any],
    etl_path: str | None = None,
    compare_source: str | None = None,
    status_text: str | None = None,
    in_progress: bool = False,
) -> str:
    metrics = result.get("metrics", {}) or {}
    trace = metrics.get("trace", {}) or {}
    io = metrics.get("io", {}) or {}
    boot = metrics.get("boot", {}) or {}
    io_percentiles = io.get("percentiles_s", {}) or {}
    launch = metrics.get("launch_latency", {}) or {}
    launch_stats = launch.get("stats", {}) or {}

    lines: list[str] = []
    if status_text:
        lines.append(f"Stage: {status_text}")
        lines.append(f"Run state: {'In progress' if in_progress else 'Complete'}")
        lines.append("")

    lines.extend([
        f"ETL file: {etl_path or metrics.get('metadata', {}).get('etl_path') or 'n/a'}",
        f"Events: {trace.get('event_count', 'n/a')}",
        f"Trace duration: {_format_seconds(trace.get('duration_s'))}",
        "Events / s: "
        + (
            f"{float(trace.get('events_per_s')):.2f}"
            if trace.get("events_per_s") is not None
            else "n/a"
        ),
        "Events / process: "
        + (
            f"{float(trace.get('events_per_process')):.2f}"
            if trace.get("events_per_process") is not None
            else "n/a"
        ),
        "Events / user process: "
        + (
            f"{float(trace.get('events_per_user_process')):.2f}"
            if trace.get("events_per_user_process") is not None
            else "n/a"
        ),
        "Processes: "
        + (
            f"{int(trace.get('process_count'))} total / "
            f"{int(trace.get('user_process_count'))} user"
            if trace.get("process_count") is not None
            and trace.get("user_process_count") is not None
            else "n/a"
        ),
        f"User process ratio: {(float(trace.get('user_process_ratio_pct', 0.0) or 0.0) * 100.0):.2f}%",
        f"Slow I/O time: {_format_seconds(io.get('slow_time_s'))}",
        "I/O throughput: "
        + (
            f"{_format_bytes(float(io.get('throughput_bytes_per_s')))} / s"
            if io.get("throughput_bytes_per_s") is not None
            else "n/a"
        ),
        "I/O bytes / user process: "
        + (
            _format_bytes(float(io.get("bytes_per_user_process")))
            if io.get("bytes_per_user_process") is not None
            else "n/a"
        ),
        f"Slow I/O %: {(float(io.get('slow_time_pct', 0.0) or 0.0) * 100.0):.2f}%",
        f"Slow ops %: {(float(io.get('slow_ops_pct', 0.0) or 0.0) * 100.0):.2f}%",
        "Slow ops / s: "
        + (
            f"{float(io.get('slow_ops_per_s')):.2f}"
            if io.get("slow_ops_per_s") is not None
            else "n/a"
        ),
        "Slow ops / user process: "
        + (
            f"{float(io.get('slow_ops_per_user_process')):.2f}"
            if io.get("slow_ops_per_user_process") is not None
            else "n/a"
        ),
        "Slow I/O s / user process: "
        + (
            _format_seconds(float(io.get("slow_time_per_user_process_s")))
            if io.get("slow_time_per_user_process_s") is not None
            else "n/a"
        ),
        "Avg slow I/O latency: "
        + (
            f"{float(io.get('slow_time_avg_ms')):.2f} ms"
            if io.get("slow_time_avg_ms") is not None
            else "n/a"
        ),
        f"I/O p95: {_format_seconds(io_percentiles.get('p95_s'))}",
        f"I/O p99: {_format_seconds(io_percentiles.get('p99_s'))}",
        f"Launch p95: {_format_seconds(launch_stats.get('p95_s'))}",
        f"Launch coverage: {(float(launch_stats.get('coverage_pct', 0.0) or 0.0) * 100.0):.2f}%",
        f"Boot duration: {_format_seconds(boot.get('boot_duration_s'))}",
        f"Boot order entries: {int(boot.get('boot_order_count', 0) or 0)}",
    ])

    if compare_source:
        lines.append(f"Baseline source: {compare_source}")

    for label, value in (
        ("Metrics JSON", result.get("metrics_path")),
        ("Baseline JSON", result.get("baseline_metrics_path")),
        ("Timeline", result.get("timeline_path")),
        ("HTML report", result.get("report_path")),
    ):
        lines.append(f"{label}: {value or 'not written'}")

    plot_paths = result.get("plot_paths", {}) or {}
    if plot_paths:
        for key, value in sorted(plot_paths.items()):
            lines.append(f"Plot ({key}): {value}")
    else:
        lines.append("Plots: not written")

    warnings = result.get("warnings", []) or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)

    comparison = result.get("comparison", {}) or {}
    if comparison:
        lines.append("")
        lines.append("Comparison deltas:")
        for key in sorted(comparison.keys()):
            entry = comparison.get(key, {}) or {}
            delta = entry.get("delta")
            pct = entry.get("pct")
            delta_text = "n/a"
            if isinstance(delta, (int, float)):
                if key.endswith("_pct"):
                    delta_text = f"{delta * 100:.2f}%"
                else:
                    delta_text = _format_seconds(float(delta))
            pct_text = "n/a"
            if isinstance(pct, (int, float)):
                pct_text = f"{pct * 100:.2f}%"
            lines.append(f"- {key}: delta={delta_text}, pct={pct_text}")

    return "\n".join(lines)


def _format_review_item(item: Any) -> str:
    if isinstance(item, dict):
        label = (
            item.get("label")
            or item.get("metric")
            or item.get("title")
            or item.get("image")
        )
        if label:
            text = str(label)
            pct = item.get("pct")
            if isinstance(pct, (int, float)):
                text += f" ({pct * 100:.2f}%)"
            return text
        return _format_json_block(item)
    return str(item)


def _format_review_diagnose(result: dict[str, Any]) -> str:
    backend = result.get("backend", {}) or {}
    insights = result.get("insights", {}) or {}
    provider = str(backend.get("provider") or "ollama").title()
    used_ollama = bool(backend.get("used_ollama"))
    status = "Connected" if used_ollama else "Heuristic fallback"

    lines = [
        f"Agentic Diagnose ({provider})",
        f"Status: {status}",
        f"Host: {backend.get('host') or 'n/a'}",
        f"Model: {backend.get('model') or 'n/a'}",
    ]
    transport = backend.get("transport")
    if transport:
        lines.append(f"Transport: {transport}")
    endpoint = backend.get("endpoint")
    if endpoint:
        lines.append(f"Endpoint: {endpoint}")
    attempted_endpoints = backend.get("attempted_endpoints")
    if isinstance(attempted_endpoints, list) and attempted_endpoints:
        lines.append(
            "Attempted endpoints: "
            + ", ".join(str(item) for item in attempted_endpoints)
        )
    attempted_models = backend.get("attempted_models")
    if isinstance(attempted_models, list) and attempted_models:
        lines.append(
            "Attempted models: " + ", ".join(str(item) for item in attempted_models)
        )
    timeout_s = backend.get("request_timeout_s")
    if isinstance(timeout_s, (int, float)):
        lines.append(f"Timeout: {float(timeout_s):.1f}s")
    parse_mode = backend.get("parse_mode")
    if parse_mode:
        lines.append(f"Response parse mode: {parse_mode}")
    if backend.get("model_alias_used"):
        lines.append("Model alias fallback: yes")
    if backend.get("cli_fallback_enabled"):
        lines.append("CLI fallback enabled: yes")
    if backend.get("cli_fallback_used"):
        lines.append("CLI fallback used: yes")
    cli_error = backend.get("cli_error")
    if cli_error:
        lines.append(f"CLI fallback error: {_sanitize_display_text(cli_error)}")

    error = backend.get("error")
    if error:
        lines.append(f"Fallback reason: {_sanitize_display_text(error)}")

    summary = insights.get("summary")
    if summary:
        lines.extend(["", "Summary", str(summary)])

    for section_title, key in (
        ("Observations", "observations"),
        ("Regressions", "regressions"),
        ("Improvements", "improvements"),
        ("Recommendations", "recommendations"),
        ("Questions", "questions"),
    ):
        items = insights.get(key)
        if not items:
            continue
        lines.extend(["", section_title])
        for item in items:
            lines.append(f"- {_format_review_item(item)}")

    return "\n".join(lines)


def _run_gui_background_task(
    task_queue: queue.Queue[tuple[str, Any]],
    worker: Callable[[], Any],
) -> None:
    try:
        task_queue.put(("success", worker()))
    except Exception as exc:
        task_queue.put(("error", exc))


def launch_standalone_gui(initial_etl_path: str | None = None) -> int:
    try:
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except Exception as exc:  # pragma: no cover - depends on runtime environment
        print(f"Tkinter is not available: {exc}", file=sys.stderr)
        return 1

    class _StandaloneGUI:
        def __init__(self) -> None:
            self.root = tk.Tk()
            self.root.title("ETL LogChecker")
            self.root.geometry("1440x960")
            self.root.minsize(1200, 820)

            self.status_var = tk.StringVar(
                value="Ready. Pick an ETL trace or metrics files to begin."
            )

            self.analyze_etl_var = tk.StringVar(value=initial_etl_path or "")
            self.auto_output_files_var = tk.BooleanVar(value=True)
            self.report_path_var = tk.StringVar()
            self.metrics_output_var = tk.StringVar()
            self.timeline_output_var = tk.StringVar()
            self.plot_dir_var = tk.StringVar()
            self.output_help_var = tk.StringVar()
            self.compare_metrics_var = tk.StringVar()
            self.compare_etl_var = tk.StringVar()
            self.tracerpt_exe_var = tk.StringVar()
            self.slow_io_ms_var = tk.IntVar(value=50)
            self.top_n_var = tk.IntVar(value=10)
            self.time_scale_var = tk.StringVar(value="auto")
            self.timeline_format_var = tk.StringVar(value="json")
            self.plot_bin_s_var = tk.DoubleVar(value=1.0)
            self.bootlog_var = tk.BooleanVar(value=False)
            self.boot_window_s_var = tk.DoubleVar(value=300.0)
            self.use_tracerpt_var = tk.BooleanVar(value=True)
            self.use_etl_observer_var = tk.BooleanVar(value=True)
            self.force_etl_observer_var = tk.BooleanVar(value=False)
            self.debug_var = tk.BooleanVar(value=False)

            self.compare_current_var = tk.StringVar()
            self.compare_baseline_var = tk.StringVar()

            self.review_current_var = tk.StringVar()
            self.review_baseline_var = tk.StringVar()
            self.review_focus_var = tk.StringVar()
            self.review_host_var = tk.StringVar(
                value=os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            )
            self.review_model_var = tk.StringVar(
                value=os.environ.get("OLLAMA_MODEL", "ministral:latest")
            )
            self.review_temperature_var = tk.DoubleVar(value=0.2)
            self.review_max_tokens_var = tk.IntVar(value=800)
            self.review_use_cli_fallback_var = tk.BooleanVar(
                value=os.environ.get("OLLAMA_USE_CLI_FALLBACK", "1").strip().lower()
                not in {"0", "false", "no", "off", ""}
            )
            self._analysis_output_controls: list[tuple[Any, Any]] = []
            self._action_buttons: list[Any] = []
            self._busy = False
            self._busy_started_at: float | None = None
            self._busy_status_text = ""
            self._analysis_live_result: dict[str, Any] = {}
            self._analysis_context: dict[str, Any] = {}

            self._configure_style(tkfont)
            self._build_layout(scrolledtext, ttk)
            self._apply_suggested_output_paths(force=False)
            self._update_output_mode_ui()

        def _configure_style(self, tkfont_module: Any) -> None:
            style = ttk.Style(self.root)
            if "clam" in style.theme_names():
                style.theme_use("clam")
            self.default_font = tkfont_module.nametofont("TkDefaultFont")
            self.default_font.configure(size=11)
            self.text_font = tkfont_module.nametofont("TkTextFont")
            self.text_font.configure(size=11)
            self.fixed_font = tkfont_module.nametofont("TkFixedFont")
            self.fixed_font.configure(size=11)
            title_font = self.default_font.copy()
            title_font.configure(size=12, weight="bold")
            style.configure("TLabelframe", padding=10)
            style.configure("TLabelframe.Label", font=title_font)
            style.configure("TButton", padding=(10, 6))
            style.configure("TNotebook.Tab", padding=(14, 8))
            style.configure(
                "Help.TLabel",
                foreground="#475569",
                justify="left",
            )
            style.configure("Section.TLabel", font=title_font)

        def _build_layout(self, scrolledtext_module: Any, ttk_module: Any) -> None:
            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(0, weight=1)
            container = ttk_module.Frame(self.root, padding=16)
            container.grid(sticky="nsew")
            container.columnconfigure(0, weight=1)
            container.rowconfigure(0, weight=1)

            notebook = ttk_module.Notebook(container)
            notebook.grid(row=0, column=0, sticky="nsew")

            analyze_tab = ttk_module.Frame(notebook, padding=12)
            compare_tab = ttk_module.Frame(notebook, padding=12)
            review_tab = ttk_module.Frame(notebook, padding=12)
            notebook.add(analyze_tab, text="Analyze ETL")
            notebook.add(compare_tab, text="Compare Metrics")
            notebook.add(review_tab, text="Agentic Diagnose")

            self._build_analyze_tab(analyze_tab, ttk_module)
            self._build_compare_tab(compare_tab, ttk_module)
            self._build_review_tab(review_tab, ttk_module)

            footer = ttk_module.Frame(container)
            footer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            footer.columnconfigure(1, weight=1)

            self.progress_bar = ttk_module.Progressbar(
                footer,
                mode="indeterminate",
                length=220,
            )
            self.progress_bar.grid(row=0, column=0, sticky="w", padx=(0, 10))

            status = ttk_module.Label(
                footer,
                textvariable=self.status_var,
                anchor="w",
            )
            status.grid(row=0, column=1, sticky="ew")

        def _build_path_row(
            self,
            parent: Any,
            row: int,
            label: str,
            variable: Any,
            mode: str,
            filetypes: list[tuple[str, str]] | None = None,
        ) -> tuple[Any, Any]:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(parent, textvariable=variable)
            entry.grid(
                row=row, column=1, sticky="ew", padx=(8, 8), pady=4
            )
            button = ttk.Button(
                parent,
                text="Browse",
                command=lambda: self._choose_path(
                    variable,
                    mode=mode,
                    filetypes=filetypes,
                ),
            )
            button.grid(row=row, column=2, sticky="ew", pady=4)
            return entry, button

        def _build_help_label(
            self,
            parent: Any,
            row: int,
            text: str | None = None,
            textvariable: Any | None = None,
            wraplength: int = 520,
            columnspan: int = 3,
        ) -> Any:
            label = ttk.Label(
                parent,
                text=text,
                textvariable=textvariable,
                style="Help.TLabel",
                wraplength=wraplength,
            )
            label.grid(
                row=row,
                column=0,
                columnspan=columnspan,
                sticky="ew",
                pady=(0, 8),
            )
            return label

        def _choose_path(
            self,
            variable: Any,
            mode: str,
            filetypes: list[tuple[str, str]] | None = None,
        ) -> None:
            current = variable.get().strip()
            initial_dir = (
                os.path.dirname(current)
                if current and os.path.dirname(current)
                else os.getcwd()
            )
            selected = ""
            if mode == "open":
                selected = filedialog.askopenfilename(
                    initialdir=initial_dir,
                    filetypes=filetypes or [("All files", "*.*")],
                )
            elif mode == "save":
                selected = filedialog.asksaveasfilename(
                    initialdir=initial_dir,
                    initialfile=os.path.basename(current) or None,
                    filetypes=filetypes or [("All files", "*.*")],
                )
            elif mode == "dir":
                selected = filedialog.askdirectory(initialdir=initial_dir)
            if selected:
                variable.set(selected)
                if variable is self.analyze_etl_var:
                    self._apply_suggested_output_paths(force=False)

        def _new_text_area(
            self,
            parent: Any,
            wrap: str = "word",
            height: int = 12,
        ) -> Any:
            font = self.text_font if wrap == "word" else self.fixed_font
            widget = scrolledtext.ScrolledText(
                parent,
                wrap=wrap,
                height=height,
                font=font,
                spacing1=2,
                spacing3=2,
            )
            widget.grid(sticky="nsew")
            return widget

        def _set_text(self, widget: Any, text: str) -> None:
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text)
            widget.configure(state="disabled")

        def _register_action_button(self, button: Any) -> Any:
            self._action_buttons.append(button)
            return button

        def _set_busy_state(
            self,
            busy: bool,
            status_text: str | None = None,
        ) -> None:
            self._busy = busy
            for button in self._action_buttons:
                if busy:
                    button.state(["disabled"])
                else:
                    button.state(["!disabled"])
            if busy:
                self._busy_started_at = time.monotonic()
                self._busy_status_text = status_text or "Working..."
                self.progress_bar.start(10)
            else:
                self._busy_started_at = None
                self._busy_status_text = ""
                self.progress_bar.stop()
            if status_text is not None:
                if busy:
                    self._refresh_busy_status(status_text)
                else:
                    self.status_var.set(status_text)

        def _format_elapsed(self) -> str:
            if self._busy_started_at is None:
                return "0s"
            elapsed_s = max(0, int(time.monotonic() - self._busy_started_at))
            hours, remainder = divmod(elapsed_s, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                return f"{hours}h {minutes:02d}m {seconds:02d}s"
            if minutes:
                return f"{minutes}m {seconds:02d}s"
            return f"{seconds}s"

        def _refresh_busy_status(self, status_text: str | None = None) -> None:
            if status_text is not None:
                self._busy_status_text = status_text
            base = self._busy_status_text or "Working..."
            if not self._busy:
                self.status_var.set(base)
                return
            self.status_var.set(f"{base} (elapsed {self._format_elapsed()})")

        def _start_background_task(
            self,
            worker: Callable[[Callable[[str], None], Callable[[str, Any], None]], Any],
            on_success: Callable[[Any], None],
            *,
            running_text: str,
            failure_title: str,
            failure_status: str,
            event_handlers: dict[str, Callable[[Any], None]] | None = None,
        ) -> None:
            if self._busy:
                self.status_var.set("A task is already running. Wait for it to finish.")
                return

            task_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
            self._set_busy_state(True, running_text)

            def _report_progress(status_text: str) -> None:
                task_queue.put(("progress", status_text))

            def _report_event(event_name: str, payload: Any) -> None:
                task_queue.put((event_name, payload))

            worker_thread = threading.Thread(
                target=_run_gui_background_task,
                args=(task_queue, lambda: worker(_report_progress, _report_event)),
                daemon=True,
            )
            worker_thread.start()

            self.root.after(
                250,
                lambda: self._poll_background_task(
                    task_queue,
                    on_success,
                    failure_title=failure_title,
                    failure_status=failure_status,
                    event_handlers=event_handlers,
                ),
            )

        def _poll_background_task(
            self,
            task_queue: queue.Queue[tuple[str, Any]],
            on_success: Callable[[Any], None],
            *,
            failure_title: str,
            failure_status: str,
            event_handlers: dict[str, Callable[[Any], None]] | None = None,
        ) -> None:
            terminal_message: tuple[str, Any] | None = None

            while True:
                try:
                    result_type, payload = task_queue.get_nowait()
                except queue.Empty:
                    break

                if result_type == "progress":
                    self._refresh_busy_status(str(payload))
                    continue
                if event_handlers and result_type in event_handlers:
                    event_handlers[result_type](payload)
                    continue
                terminal_message = (result_type, payload)
                break

            if terminal_message is None:
                self._refresh_busy_status()
                self.root.after(
                    250,
                    lambda: self._poll_background_task(
                        task_queue,
                        on_success,
                        failure_title=failure_title,
                        failure_status=failure_status,
                        event_handlers=event_handlers,
                    ),
                )
                return

            result_type, payload = terminal_message
            self._set_busy_state(False)
            if result_type == "error":
                self.status_var.set(failure_status)
                messagebox.showerror(failure_title, str(payload))
                return

            try:
                on_success(payload)
            except Exception as exc:
                self.status_var.set(failure_status)
                messagebox.showerror(failure_title, str(exc))

        def _build_analyze_tab(
            self,
            parent: Any,
            ttk_module: Any,
        ) -> None:
            parent.columnconfigure(1, weight=1)
            parent.rowconfigure(0, weight=1)

            controls = ttk_module.Frame(parent)
            controls.grid(row=0, column=0, sticky="ns", padx=(0, 14))

            source_section = ttk_module.LabelFrame(controls, text="Trace & Baseline")
            source_section.grid(row=0, column=0, sticky="ew")
            source_section.columnconfigure(1, weight=1)
            self._build_help_label(
                source_section,
                0,
                text=(
                    "Choose the ETL you want to inspect. Baseline inputs are optional and "
                    "are only used when you want a delta comparison."
                ),
                wraplength=360,
            )
            row = 1
            self._build_path_row(
                source_section,
                row,
                "ETL trace",
                self.analyze_etl_var,
                "open",
                [("ETL files", "*.etl"), ("All files", "*.*")],
            )
            row += 1
            self._build_path_row(
                source_section,
                row,
                "Baseline metrics",
                self.compare_metrics_var,
                "open",
                [("JSON", "*.json"), ("All files", "*.*")],
            )
            row += 1
            self._build_path_row(
                source_section,
                row,
                "Baseline ETL",
                self.compare_etl_var,
                "open",
                [("ETL files", "*.etl"), ("All files", "*.*")],
            )

            output_section = ttk_module.LabelFrame(controls, text="Output Files")
            output_section.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            output_section.columnconfigure(1, weight=1)
            self._build_help_label(
                output_section,
                0,
                textvariable=self.output_help_var,
                wraplength=360,
            )
            ttk_module.Checkbutton(
                output_section,
                text="Auto-create report, metrics, timeline, and plot outputs",
                variable=self.auto_output_files_var,
                command=self._toggle_auto_output_mode,
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
            self._analysis_output_controls = []
            row = 2
            entry, button = self._build_path_row(
                output_section,
                row,
                "Report output",
                self.report_path_var,
                "save",
                [("HTML", "*.html"), ("All files", "*.*")],
            )
            self._analysis_output_controls.append((entry, button))
            row += 1
            entry, button = self._build_path_row(
                output_section,
                row,
                "Metrics output",
                self.metrics_output_var,
                "save",
                [("JSON", "*.json"), ("All files", "*.*")],
            )
            self._analysis_output_controls.append((entry, button))
            row += 1
            entry, button = self._build_path_row(
                output_section,
                row,
                "Timeline output",
                self.timeline_output_var,
                "save",
                [
                    ("JSON", "*.json"),
                    ("CSV", "*.csv"),
                    ("All files", "*.*"),
                ],
            )
            self._analysis_output_controls.append((entry, button))
            row += 1
            entry, button = self._build_path_row(
                output_section,
                row,
                "Plot directory",
                self.plot_dir_var,
                "dir",
            )
            self._analysis_output_controls.append((entry, button))

            settings_section = ttk_module.LabelFrame(controls, text="Settings")
            settings_section.grid(row=2, column=0, sticky="ew", pady=(12, 0))
            settings_section.columnconfigure(1, weight=1)
            self._build_help_label(
                settings_section,
                0,
                text=(
                    "Use the defaults first. Increase Top N for larger tables, and only "
                    "enable debug logging when you need parser progress details."
                ),
                wraplength=360,
            )
            self._build_path_row(
                settings_section,
                1,
                "tracerpt.exe",
                self.tracerpt_exe_var,
                "open",
                [("Executable", "*.exe"), ("All files", "*.*")],
            )

            numeric_frame = ttk_module.Frame(settings_section)
            numeric_frame.grid(
                row=2,
                column=0,
                columnspan=3,
                sticky="ew",
                pady=(8, 0),
            )
            for idx in range(6):
                numeric_frame.columnconfigure(idx, weight=1)

            ttk_module.Label(numeric_frame, text="Slow I/O (ms)").grid(
                row=0, column=0, sticky="w"
            )
            ttk_module.Entry(
                numeric_frame,
                textvariable=self.slow_io_ms_var,
                width=10,
            ).grid(row=1, column=0, sticky="ew", padx=(0, 8))

            ttk_module.Label(numeric_frame, text="Top N").grid(row=0, column=1, sticky="w")
            ttk_module.Entry(
                numeric_frame,
                textvariable=self.top_n_var,
                width=10,
            ).grid(row=1, column=1, sticky="ew", padx=(0, 8))

            ttk_module.Label(numeric_frame, text="Time scale").grid(
                row=0, column=2, sticky="w"
            )
            ttk_module.Entry(
                numeric_frame,
                textvariable=self.time_scale_var,
                width=10,
            ).grid(row=1, column=2, sticky="ew", padx=(0, 8))

            ttk_module.Label(numeric_frame, text="Timeline format").grid(
                row=0, column=3, sticky="w"
            )
            ttk_module.Combobox(
                numeric_frame,
                textvariable=self.timeline_format_var,
                values=("json", "csv"),
                state="readonly",
                width=10,
            ).grid(row=1, column=3, sticky="ew", padx=(0, 8))

            ttk_module.Label(numeric_frame, text="Plot bin (s)").grid(
                row=0, column=4, sticky="w"
            )
            ttk_module.Entry(
                numeric_frame,
                textvariable=self.plot_bin_s_var,
                width=10,
            ).grid(row=1, column=4, sticky="ew", padx=(0, 8))

            ttk_module.Label(numeric_frame, text="Boot window (s)").grid(
                row=0, column=5, sticky="w"
            )
            ttk_module.Entry(
                numeric_frame,
                textvariable=self.boot_window_s_var,
                width=10,
            ).grid(row=1, column=5, sticky="ew")

            flag_frame = ttk_module.Frame(settings_section)
            flag_frame.grid(
                row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0)
            )
            for idx in range(5):
                flag_frame.columnconfigure(idx, weight=1)

            ttk_module.Checkbutton(
                flag_frame,
                text="Bootlog",
                variable=self.bootlog_var,
            ).grid(row=0, column=0, sticky="w")
            ttk_module.Checkbutton(
                flag_frame,
                text="Use tracerpt fallback",
                variable=self.use_tracerpt_var,
            ).grid(row=0, column=1, sticky="w")
            ttk_module.Checkbutton(
                flag_frame,
                text="Use ETL observer",
                variable=self.use_etl_observer_var,
            ).grid(row=0, column=2, sticky="w")
            ttk_module.Checkbutton(
                flag_frame,
                text="Force ETL observer",
                variable=self.force_etl_observer_var,
            ).grid(row=0, column=3, sticky="w")
            ttk_module.Checkbutton(
                flag_frame,
                text="Debug logging",
                variable=self.debug_var,
            ).grid(row=0, column=4, sticky="w")

            button_frame = ttk_module.Frame(controls)
            button_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            button_frame.columnconfigure(0, weight=1)
            run_button = ttk_module.Button(
                button_frame,
                text="Run Analysis",
                command=self._run_analysis,
            )
            run_button.grid(row=0, column=0, sticky="ew")
            self._register_action_button(run_button)

            results = ttk_module.Notebook(parent)
            results.grid(row=0, column=1, sticky="nsew")

            summary_frame = ttk_module.Frame(results, padding=10)
            metrics_frame = ttk_module.Frame(results, padding=10)
            baseline_frame = ttk_module.Frame(results, padding=10)
            comparison_frame = ttk_module.Frame(results, padding=10)
            report_frame = ttk_module.Frame(results, padding=10)
            for frame in (
                summary_frame,
                metrics_frame,
                baseline_frame,
                comparison_frame,
                report_frame,
            ):
                frame.columnconfigure(0, weight=1)
                frame.rowconfigure(0, weight=1)

            results.add(summary_frame, text="Summary")
            results.add(metrics_frame, text="Metrics JSON")
            results.add(baseline_frame, text="Baseline JSON")
            results.add(comparison_frame, text="Comparison")
            results.add(report_frame, text="Report HTML")

            self.analysis_summary_text = self._new_text_area(summary_frame, height=18)
            self.analysis_metrics_text = self._new_text_area(metrics_frame, wrap="char")
            self.analysis_baseline_text = self._new_text_area(
                baseline_frame, wrap="char"
            )
            self.analysis_comparison_text = self._new_text_area(
                comparison_frame, wrap="char"
            )
            self.analysis_report_text = self._new_text_area(report_frame, wrap="char")

        def _build_compare_tab(
            self,
            parent: Any,
            ttk_module: Any,
        ) -> None:
            parent.columnconfigure(0, weight=1)
            parent.rowconfigure(2, weight=1)

            self._build_help_label(
                parent,
                0,
                text=(
                    "Compare two existing metrics JSON files. This does not re-run ETL "
                    "analysis; it only scores deltas between saved outputs."
                ),
                wraplength=960,
                columnspan=1,
            )

            controls = ttk_module.LabelFrame(parent, text="Compare Existing Metrics")
            controls.grid(row=1, column=0, sticky="ew")
            controls.columnconfigure(1, weight=1)

            self._build_path_row(
                controls,
                0,
                "Current metrics",
                self.compare_current_var,
                "open",
                [("JSON", "*.json"), ("All files", "*.*")],
            )
            self._build_path_row(
                controls,
                1,
                "Baseline metrics",
                self.compare_baseline_var,
                "open",
                [("JSON", "*.json"), ("All files", "*.*")],
            )

            compare_button = ttk_module.Button(
                controls,
                text="Compare Metrics",
                command=self._run_compare_metrics,
            )
            compare_button.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
            self._register_action_button(compare_button)

            output_frame = ttk_module.Frame(parent, padding=(0, 12, 0, 0))
            output_frame.grid(row=2, column=0, sticky="nsew")
            output_frame.columnconfigure(0, weight=1)
            output_frame.rowconfigure(0, weight=1)
            self.compare_output_text = self._new_text_area(output_frame, wrap="char")

        def _build_review_tab(
            self,
            parent: Any,
            ttk_module: Any,
        ) -> None:
            parent.columnconfigure(0, weight=1)
            parent.rowconfigure(2, weight=1)

            self._build_help_label(
                parent,
                0,
                text=(
                    "Agentic Diagnose runs the Ollama-backed review when available. If "
                    "Ollama is not reachable, the app shows the built-in heuristic "
                    "fallback and explains why."
                ),
                wraplength=960,
                columnspan=1,
            )

            controls = ttk_module.LabelFrame(parent, text="Agentic Diagnose With Ollama")
            controls.grid(row=1, column=0, sticky="ew")
            controls.columnconfigure(1, weight=1)

            self._build_path_row(
                controls,
                0,
                "Current metrics",
                self.review_current_var,
                "open",
                [("JSON", "*.json"), ("All files", "*.*")],
            )
            self._build_path_row(
                controls,
                1,
                "Baseline metrics",
                self.review_baseline_var,
                "open",
                [("JSON", "*.json"), ("All files", "*.*")],
            )

            ttk_module.Label(controls, text="Focus prompt").grid(
                row=2, column=0, sticky="w", pady=4
            )
            ttk_module.Entry(
                controls,
                textvariable=self.review_focus_var,
            ).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4)

            settings = ttk_module.Frame(controls)
            settings.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            for idx in range(4):
                settings.columnconfigure(idx, weight=1)

            ttk_module.Label(settings, text="Ollama host").grid(
                row=0, column=0, sticky="w"
            )
            ttk_module.Entry(
                settings,
                textvariable=self.review_host_var,
            ).grid(row=1, column=0, sticky="ew", padx=(0, 8))

            ttk_module.Label(settings, text="Model").grid(row=0, column=1, sticky="w")
            ttk_module.Entry(
                settings,
                textvariable=self.review_model_var,
            ).grid(row=1, column=1, sticky="ew", padx=(0, 8))

            ttk_module.Label(settings, text="Temperature").grid(
                row=0, column=2, sticky="w"
            )
            ttk_module.Entry(
                settings,
                textvariable=self.review_temperature_var,
            ).grid(row=1, column=2, sticky="ew", padx=(0, 8))

            ttk_module.Label(settings, text="Max tokens").grid(
                row=0, column=3, sticky="w"
            )
            ttk_module.Entry(
                settings,
                textvariable=self.review_max_tokens_var,
            ).grid(row=1, column=3, sticky="ew")

            ttk_module.Checkbutton(
                controls,
                text="Use local ollama CLI fallback if HTTP endpoints fail",
                variable=self.review_use_cli_fallback_var,
            ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

            review_button = ttk_module.Button(
                controls,
                text="Run Agentic Diagnose",
                command=self._run_review,
            )
            review_button.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
            self._register_action_button(review_button)

            output_frame = ttk_module.Frame(parent, padding=(0, 12, 0, 0))
            output_frame.grid(row=2, column=0, sticky="nsew")
            output_frame.columnconfigure(0, weight=1)
            output_frame.rowconfigure(0, weight=1)
            self.review_output_text = self._new_text_area(output_frame, wrap="word")

        def _apply_suggested_output_paths(self, force: bool) -> None:
            if not self.auto_output_files_var.get() and not force:
                self._refresh_output_help()
                return
            suggestions = _suggest_output_paths(self.analyze_etl_var.get().strip() or None)
            for key, variable in (
                ("report_path", self.report_path_var),
                ("metrics_output_path", self.metrics_output_var),
                ("timeline_output_path", self.timeline_output_var),
                ("plot_dir", self.plot_dir_var),
            ):
                if force or not variable.get().strip():
                    variable.set(suggestions.get(key, ""))
            self._refresh_output_help()

        def _refresh_output_help(self) -> None:
            etl_path = self.analyze_etl_var.get().strip() or None
            if self.auto_output_files_var.get():
                if not etl_path:
                    self.output_help_var.set(
                        "Auto mode is on. Choose an ETL and the app will create "
                        "report, metrics, timeline, and plot outputs in a timestamped folder."
                    )
                    return
                suggested = _suggest_output_paths(etl_path)
                folder_name = os.path.basename(
                    os.path.dirname(suggested["report_path"])
                )
                self.output_help_var.set(
                    "Auto mode is on. Files will be created automatically in "
                    f"{folder_name}: "
                    f"{os.path.basename(suggested['report_path'])}, "
                    f"{os.path.basename(suggested['metrics_output_path'])}, "
                    f"{os.path.basename(suggested['timeline_output_path'])}, and "
                    f"{os.path.basename(suggested['plot_dir'])}/."
                )
                return
            self.output_help_var.set(
                "Auto mode is off. Edit the paths below to control which artifacts are "
                "written. Leave a field blank to skip that output."
            )

        def _update_output_mode_ui(self) -> None:
            auto_mode = bool(self.auto_output_files_var.get())
            for entry, button in self._analysis_output_controls:
                if auto_mode:
                    entry.state(["readonly"])
                    button.state(["disabled"])
                else:
                    entry.state(["!readonly", "!disabled"])
                    button.state(["!disabled"])
            self._refresh_output_help()

        def _toggle_auto_output_mode(self) -> None:
            if self.auto_output_files_var.get():
                self._apply_suggested_output_paths(force=True)
            self._update_output_mode_ui()

        def _render_analysis_panels(
            self,
            result: dict[str, Any],
            *,
            status_text: str | None,
            in_progress: bool,
        ) -> None:
            etl_path = self._analysis_context.get("etl_path")
            compare_source = self._analysis_context.get("compare_source")
            report_requested = bool(self._analysis_context.get("report_requested"))

            baseline_data = result.get("baseline_metrics")
            if (
                baseline_data is None
                and not in_progress
                and self.compare_metrics_var.get().strip()
            ):
                try:
                    with open(
                        self.compare_metrics_var.get().strip(),
                        "r",
                        encoding="utf-8",
                    ) as handle:
                        baseline_data = json.load(handle)
                except Exception:
                    baseline_data = None

            self._set_text(
                self.analysis_summary_text,
                _build_analysis_summary(
                    result,
                    etl_path=etl_path,
                    compare_source=compare_source,
                    status_text=status_text,
                    in_progress=in_progress,
                ),
            )

            metrics_data = result.get("metrics")
            if metrics_data:
                metrics_text = _format_json_block(metrics_data)
            elif in_progress:
                metrics_text = "Waiting for primary ETL metrics..."
            else:
                metrics_text = _format_json_block({})
            self._set_text(self.analysis_metrics_text, metrics_text)

            if baseline_data:
                baseline_text = _format_json_block(baseline_data)
            elif compare_source and in_progress:
                baseline_text = "Waiting for baseline metrics..."
            else:
                baseline_text = _format_json_block({})
            self._set_text(self.analysis_baseline_text, baseline_text)

            comparison_data = result.get("comparison")
            if comparison_data:
                comparison_text = _format_json_block(comparison_data)
            elif compare_source and in_progress:
                comparison_text = "Waiting for comparison results..."
            else:
                comparison_text = _format_json_block({})
            self._set_text(self.analysis_comparison_text, comparison_text)

            report_text = result.get("report_html")
            if report_text:
                report_block = _format_html_block(report_text)
            elif not in_progress:
                report_block = (
                    _format_html_block(_read_text_file(result.get("report_path")))
                    or "No HTML report was generated."
                )
            elif report_requested:
                report_block = "Waiting for HTML rendering to complete..."
            else:
                report_block = "No HTML report requested for this run."
            self._set_text(self.analysis_report_text, report_block)

            self._sync_followup_paths(result)

            if comparison_data:
                self._set_text(
                    self.compare_output_text,
                    _format_json_block({"deltas": comparison_data}),
                )

        def _reset_analysis_progress(
            self,
            *,
            etl_path: str,
            compare_source: str | None,
            report_requested: bool,
            running_text: str,
        ) -> None:
            self._analysis_context = {
                "etl_path": etl_path,
                "compare_source": compare_source,
                "report_requested": report_requested,
            }
            self._analysis_live_result = {
                "warnings": [],
                "plot_paths": {},
            }
            self._render_analysis_panels(
                self._analysis_live_result,
                status_text=running_text,
                in_progress=True,
            )

        def _apply_analysis_progress(self, payload: dict[str, Any]) -> None:
            self._analysis_live_result = _merge_analysis_progress_result(
                self._analysis_live_result,
                payload.get("updates"),
            )
            self._render_analysis_panels(
                self._analysis_live_result,
                status_text=str(payload.get("status_text") or "Working..."),
                in_progress=True,
            )

        def _sync_followup_paths(self, result: dict[str, Any]) -> None:
            metrics_path = result.get("metrics_path")
            if metrics_path:
                self.compare_current_var.set(metrics_path)
                self.review_current_var.set(metrics_path)

            baseline_source = (
                result.get("baseline_metrics_path")
                or self.compare_metrics_var.get().strip()
                or ""
            )
            if baseline_source:
                self.compare_baseline_var.set(baseline_source)
                self.review_baseline_var.set(baseline_source)

        def _apply_analysis_result(
            self,
            result: dict[str, Any],
            *,
            etl_path: str,
            compare_source: str | None,
        ) -> None:
            self._analysis_context = {
                "etl_path": etl_path,
                "compare_source": compare_source,
                "report_requested": bool(result.get("report_path") or self.report_path_var.get().strip()),
            }
            self._analysis_live_result = _merge_analysis_progress_result(
                self._analysis_live_result,
                result,
            )
            self._render_analysis_panels(
                self._analysis_live_result,
                status_text="Analysis complete.",
                in_progress=False,
            )
            self.status_var.set("Analysis complete.")

        def _apply_compare_metrics_result(self, result: dict[str, Any]) -> None:
            self._set_text(self.compare_output_text, _format_json_block(result))
            self.status_var.set("Metrics comparison complete.")

        def _apply_review_result(self, result: dict[str, Any]) -> None:
            self._set_text(self.review_output_text, _format_review_diagnose(result))
            if result.get("heuristic_fallback"):
                self.status_var.set("Agentic diagnose complete (heuristic fallback).")
            else:
                self.status_var.set("Agentic diagnose complete (Ollama connected).")

        def _run_analysis(self) -> None:
            etl_path = self.analyze_etl_var.get().strip()
            if not etl_path:
                messagebox.showerror("Missing ETL", "Choose an ETL trace first.")
                return

            try:
                compare_metrics_path = self.compare_metrics_var.get().strip() or None
                compare_etl_path = self.compare_etl_var.get().strip() or None
                compare_source = compare_metrics_path or compare_etl_path
                outputs = _resolve_analysis_output_paths(
                    etl_path=etl_path,
                    auto_generate=bool(self.auto_output_files_var.get()),
                    report_path=self.report_path_var.get().strip() or None,
                    metrics_output_path=self.metrics_output_var.get().strip() or None,
                    timeline_output_path=self.timeline_output_var.get().strip() or None,
                    plot_dir=self.plot_dir_var.get().strip() or None,
                )
                timeline_format = self.timeline_format_var.get().strip() or "json"
                plot_bin_s = float(self.plot_bin_s_var.get())
                slow_io_ms = int(self.slow_io_ms_var.get())
                top_n = int(self.top_n_var.get())
                time_scale = self.time_scale_var.get().strip() or "auto"
                bootlog = bool(self.bootlog_var.get())
                boot_window_s = float(self.boot_window_s_var.get())
                debug = bool(self.debug_var.get())
                progress_every = 200000 if debug else 0
                tracerpt_exe = self.tracerpt_exe_var.get().strip() or None
                use_tracerpt = bool(self.use_tracerpt_var.get())
                use_etl_observer = bool(self.use_etl_observer_var.get())
                force_etl_observer = bool(self.force_etl_observer_var.get())
            except Exception as exc:
                self.status_var.set("Analysis failed.")
                messagebox.showerror("Analysis failed", str(exc))
                return

            self._reset_analysis_progress(
                etl_path=etl_path,
                compare_source=compare_source or None,
                report_requested=bool(outputs["report_path"]),
                running_text="Preparing ETL analysis...",
            )

            def _worker(
                report_progress: Callable[[str], None],
                report_event: Callable[[str, Any], None],
            ) -> dict[str, Any]:
                return run_analysis_job(
                    etl_path=etl_path,
                    report_path=outputs["report_path"],
                    metrics_output_path=outputs["metrics_output_path"],
                    compare_metrics_path=compare_metrics_path,
                    compare_etl_path=compare_etl_path,
                    timeline_output_path=outputs["timeline_output_path"],
                    timeline_format=timeline_format,
                    plot_dir=outputs["plot_dir"],
                    plot_bin_s=plot_bin_s,
                    slow_io_ms=slow_io_ms,
                    top_n=top_n,
                    time_scale=time_scale,
                    bootlog=bootlog,
                    boot_window_s=boot_window_s,
                    debug=debug,
                    progress_every=progress_every,
                    tracerpt_exe=tracerpt_exe,
                    use_tracerpt=use_tracerpt,
                    use_etl_observer=use_etl_observer,
                    force_etl_observer=force_etl_observer,
                    return_metrics=True,
                    return_baseline_metrics=True,
                    progress_callback=report_progress,
                    progress_event_callback=lambda payload: report_event(
                        "analysis_progress", payload
                    ),
                )

            self._start_background_task(
                _worker,
                lambda result: self._apply_analysis_result(
                    result,
                    etl_path=etl_path,
                    compare_source=compare_source or None,
                ),
                running_text="Preparing ETL analysis...",
                failure_title="Analysis failed",
                failure_status="Analysis failed.",
                event_handlers={"analysis_progress": self._apply_analysis_progress},
            )

        def _run_compare_metrics(self) -> None:
            current_path = self.compare_current_var.get().strip()
            baseline_path = self.compare_baseline_var.get().strip()
            if not current_path or not baseline_path:
                messagebox.showerror(
                    "Missing metrics",
                    "Choose both current and baseline metrics JSON files.",
                )
                return

            def _worker(
                report_progress: Callable[[str], None],
                _report_event: Callable[[str, Any], None],
            ) -> dict[str, Any]:
                report_progress("Loading metrics files for comparison...")
                from etl_agent import compare_and_score

                report_progress("Scoring metric deltas...")
                return compare_and_score(current_path, baseline_path)

            self._start_background_task(
                _worker,
                self._apply_compare_metrics_result,
                running_text="Preparing metrics comparison...",
                failure_title="Metrics comparison failed",
                failure_status="Metrics comparison failed.",
            )

        def _run_review(self) -> None:
            current_path = self.review_current_var.get().strip()
            baseline_path = self.review_baseline_var.get().strip() or None
            if not current_path:
                messagebox.showerror(
                    "Missing metrics",
                    "Choose the current metrics JSON file first.",
                )
                return

            try:
                ollama_host = self.review_host_var.get().strip() or None
                model = self.review_model_var.get().strip() or None
                temperature = float(self.review_temperature_var.get())
                max_tokens = int(self.review_max_tokens_var.get())
                focus = self.review_focus_var.get().strip() or None
                use_cli_fallback = bool(self.review_use_cli_fallback_var.get())
            except Exception as exc:
                self.status_var.set("Agentic diagnose failed.")
                messagebox.showerror("Agentic diagnose failed", str(exc))
                return

            def _worker(
                report_progress: Callable[[str], None],
                _report_event: Callable[[str, Any], None],
            ) -> dict[str, Any]:
                report_progress("Preparing Agentic Diagnose request...")
                from etl_agent import review_with_llm

                report_progress("Running Agentic Diagnose...")
                return review_with_llm(
                    current=current_path,
                    baseline=baseline_path,
                    ollama_host=ollama_host,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    focus=focus,
                    return_raw=False,
                    use_cli_fallback=use_cli_fallback,
                )

            self._start_background_task(
                _worker,
                self._apply_review_result,
                running_text="Preparing Agentic Diagnose...",
                failure_title="Agentic diagnose failed",
                failure_status="Agentic diagnose failed.",
            )

    app = _StandaloneGUI()
    app.root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Windows WPR .etl files.")
    parser.add_argument("etl_path", nargs="?", help="Path to the .etl file")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the standalone GUI.",
    )
    parser.add_argument(
        "--gui-quickstart",
        action="store_true",
        help="Print the GUI quick start guide and exit.",
    )
    parser.add_argument("--output", help="Output path (default: stdout)")
    parser.add_argument("--xml-output", help="Write a full XML event dump to path")
    parser.add_argument("--format", choices=["auto", "pandas", "table"], default="auto")
    parser.add_argument("--sort", choices=["cpu", "net", "pid"], default="cpu")
    parser.add_argument("--max-rows", type=int, default=50)
    parser.add_argument("--tracerpt-exe", help="Override tracerpt.exe path")
    parser.add_argument("--no-tracerpt", action="store_true", help="Disable tracerpt fallback")
    parser.add_argument(
        "--no-etl-observer",
        action="store_true",
        help="Disable etl observer fallback (loads entire file into memory).",
    )
    parser.add_argument(
        "--force-etl-observer",
        action="store_true",
        help="Force etl observer fallback even for large ETL files.",
    )
    parser.add_argument("--report", help="Write a self-contained HTML report to path")
    parser.add_argument("--metrics-output", help="Write metrics JSON to path")
    compare_group = parser.add_mutually_exclusive_group()
    compare_group.add_argument(
        "--compare", help="Compare against a baseline metrics JSON file"
    )
    compare_group.add_argument(
        "--compare-etl", help="Compare against a baseline ETL file"
    )
    parser.add_argument("--slow-io-ms", type=int, default=50, help="Slow I/O threshold in ms")
    parser.add_argument("--top-n", type=int, default=10, help="Top N rows in report sections")
    parser.add_argument(
        "--time-scale",
        default="auto",
        help="Timestamp scale override (auto|ns|us|ms|s|float seconds per tick)",
    )
    parser.add_argument("--timeline-output", help="Write process timeline to path")
    parser.add_argument(
        "--timeline-format",
        choices=["csv", "json"],
        default="json",
        help="Timeline output format (default: json)",
    )
    parser.add_argument("--plot-dir", help="Write matplotlib plots to this directory")
    parser.add_argument(
        "--plot-bin-s",
        type=float,
        default=1.0,
        help="Time bin size for plots in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--bootlog",
        action="store_true",
        help="Enable bootlog-specific metrics and report sections",
    )
    parser.add_argument(
        "--boot-window-s",
        type=float,
        default=300.0,
        help="Boot window length for boot order capture (default: 300s)",
    )
    parser.add_argument(
        "--ux-progress-every",
        type=int,
        default=200000,
        help="Log UX analysis progress every N events (debug only)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.gui_quickstart:
        print(render_gui_quickstart_text())
        return 0

    if args.gui:
        return launch_standalone_gui(args.etl_path)

    if not args.etl_path:
        print("ETL file path is required unless --gui is used.", file=sys.stderr)
        return 2

    if args.no_etl_observer and args.force_etl_observer:
        print(
            "Cannot use --no-etl-observer with --force-etl-observer.",
            file=sys.stderr,
        )
        return 1

    if not os.path.isfile(args.etl_path):
        print(f"ETL file not found: {args.etl_path}", file=sys.stderr)
        return 1
    if args.compare_etl and not os.path.isfile(args.compare_etl):
        print(f"Baseline ETL file not found: {args.compare_etl}", file=sys.stderr)
        return 1

    if args.plot_bin_s <= 0:
        print("--plot-bin-s must be greater than 0.", file=sys.stderr)
        return 1
    if args.boot_window_s <= 0:
        print("--boot-window-s must be greater than 0.", file=sys.stderr)
        return 1

    ux_required = bool(
        args.report
        or args.metrics_output
        or args.compare
        or args.compare_etl
        or args.timeline_output
        or args.plot_dir
        or args.bootlog
    )
    if ux_required:
        metrics_path = args.metrics_output
        report_path = args.report
        if args.compare_etl and not report_path:
            report_path = "report_compare.html"
        if not metrics_path:
            if report_path:
                report_dir = os.path.dirname(report_path) or "."
                metrics_path = os.path.join(report_dir, "etl_metrics.json")
            else:
                metrics_path = "etl_metrics.json"

        try:
            run_analysis_job(
                etl_path=args.etl_path,
                report_path=report_path,
                metrics_output_path=metrics_path,
                compare_metrics_path=args.compare,
                compare_etl_path=args.compare_etl,
                timeline_output_path=args.timeline_output,
                timeline_format=args.timeline_format,
                plot_dir=args.plot_dir,
                plot_bin_s=args.plot_bin_s,
                slow_io_ms=args.slow_io_ms,
                top_n=args.top_n,
                time_scale=args.time_scale,
                bootlog=args.bootlog,
                boot_window_s=args.boot_window_s,
                debug=args.debug,
                progress_every=args.ux_progress_every,
                tracerpt_exe=args.tracerpt_exe,
                use_tracerpt=not args.no_tracerpt,
                use_etl_observer=not args.no_etl_observer,
                force_etl_observer=args.force_etl_observer,
                return_metrics=False,
                return_baseline_metrics=False,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:  # pragma: no cover - unexpected
            print(f"Unhandled error: {exc}", file=sys.stderr)
            return 1

        return 0

    try:
        timestamp_scale, _ = _resolve_time_scale(args.etl_path, args.time_scale)
    except ValueError as exc:
        print(f"Invalid --time-scale: {exc}", file=sys.stderr)
        return 1

    analyzer = ETLAnalyzer(
        args.etl_path,
        tracerpt_exe=args.tracerpt_exe,
        use_tracerpt=not args.no_tracerpt,
        debug=args.debug,
        use_etl_observer=not args.no_etl_observer,
        force_etl_observer=args.force_etl_observer,
        timestamp_scale=timestamp_scale,
    )

    try:
        rows = analyzer.analyze(xml_output=args.xml_output)
    except OSError as exc:
        print(f"Failed to read ETL file: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - unexpected
        print(f"Unhandled error: {exc}", file=sys.stderr)
        return 1

    output = _render_output(rows, args.format, args.sort, args.max_rows)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
            handle.write("\n")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
