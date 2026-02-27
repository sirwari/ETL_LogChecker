#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils
from collections import defaultdict
from typing import Any, Iterable, Iterator


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


class ETLAnalyzer:
    KERNEL_PROCESS_GUID = "22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716"
    KERNEL_PROCESS_NAME = "Microsoft-Windows-Kernel-Process"
    KERNEL_NETWORK_NAME = "Microsoft-Windows-Kernel-Network"
    OBSERVER_MAX_BYTES = 512 * 1024 * 1024
    PROGRESS_EVERY = 100_000

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

        with tempfile.TemporaryDirectory(prefix="etl_tracerpt_") as temp_dir:
            csv_path = os.path.join(temp_dir, "trace.csv")
            xml_path = os.path.join(temp_dir, "trace.xml")

            if self._run_tracerpt("CSV", csv_path):
                yield from self._iter_tracerpt_csv(csv_path)
                return

            if self._run_tracerpt("XML", xml_path):
                yield from self._iter_tracerpt_xml(xml_path)
                return

        raise RuntimeError("tracerpt.exe failed to produce CSV or XML output.")

    def _run_tracerpt(self, fmt: str, output_path: str) -> bool:
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
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "tracerpt.exe not found on PATH. Set --tracerpt-exe to a full path."
            ) from exc
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

        if "send" in event_text:
            self.net_sent_by_pid[pid] += size
        elif "recv" in event_text or "receive" in event_text:
            self.net_recv_by_pid[pid] += size
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
    ) -> None:
        self.etl_path = etl_path
        self.slow_io_ms = slow_io_ms
        self.top_n = top_n
        self.debug = debug
        self.progress_every = progress_every
        self._scaler = TimestampScaler(scale=timestamp_scale)

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

        metrics = {
            "metadata": {
                "etl_path": self.etl_path,
                "generated_at": dt.datetime.utcnow().isoformat() + "Z",
                "tool_version": TOOL_VERSION,
                "slow_io_ms": self.slow_io_ms,
            },
            "trace": {
                "start_ts": self.start_ts,
                "end_ts": self.end_ts,
                "duration_s": trace_duration,
                "event_count": self.event_count,
            },
            "boot": self.boot_milestones,
            "launch_latency": {
                "top": launch_top,
                "stats": latency_stats,
            },
            "io": {
                "total_ops": self.total_io_ops,
                "total_bytes": self.total_io_bytes,
                "slow_ops": self.slow_io_ops,
                "slow_time_s": self.total_slow_io_time_s,
                "slow_time_pct": _safe_div(self.total_slow_io_time_s, trace_duration)
                if trace_duration > 0
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
    parts = [f'<svg viewBox="0 0 {width} {height}">']
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
        label = _html_escape(str(item.get(label_key, "")))
        bar_height = (value / max_value) * chart_height
        x = padding + idx * bar_width
        y = padding + (chart_height - bar_height)
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width * 0.7:.1f}" '
            f'height="{bar_height:.1f}" rx="4" fill="url(#barGradient)"/>'
        )
        parts.append(
            f'<text x="{x + bar_width * 0.35:.1f}" y="{height - 8}" '
            f'text-anchor="middle" class="chart-label">{label}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width * 0.35:.1f}" y="{y - 6:.1f}" '
            f'text-anchor="middle" class="chart-value">{value:.2f}{unit}</text>'
        )
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
    parts = ['<div class="timeline">', f'<svg viewBox="0 0 {width} {height}">']
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
    parts = [f'<svg viewBox="0 0 {width} {height}">']
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
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


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
        "explorer_start_s": (
            get_path(current, "boot", "explorer_start_s"),
            get_path(baseline, "boot", "explorer_start_s"),
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


def _render_report(metrics: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    boot = metrics.get("boot", {})
    io = metrics.get("io", {})
    io_percentiles = io.get("percentiles_s", {})
    comparison = _compare_metrics(metrics, baseline) if baseline else None

    slow_pct = io.get("slow_time_pct", 0.0) * 100.0
    duration_s = metrics.get("trace", {}).get("duration_s", 0.0) or 0.0

    launch_top = metrics.get("launch_latency", {}).get("top", [])
    slow_top = metrics.get("top_processes", {}).get("by_slow_time", [])
    slow_ops_top = metrics.get("top_processes", {}).get("by_slow_ops", [])
    io_bytes_top = metrics.get("top_processes", {}).get("by_io_bytes", [])
    top_files = metrics.get("top_files", [])

    comparison_rows = []
    if comparison:
        for label, key, formatter in (
            ("Trace duration", "duration_s", _format_seconds),
            ("Slow I/O time", "slow_io_time_s", _format_seconds),
            ("Slow I/O %", "slow_io_pct", lambda v: f"{v * 100:.2f}%"),
            ("I/O p95", "io_p95_s", _format_seconds),
            ("I/O p99", "io_p99_s", _format_seconds),
            ("Explorer start", "explorer_start_s", _format_seconds),
        ):
            entry = comparison.get(key, {})
            cur = entry.get("current")
            base = entry.get("baseline")
            delta = entry.get("delta")
            pct = entry.get("pct")
            comparison_rows.append(
                [
                    label,
                    formatter(cur) if cur is not None else "n/a",
                    formatter(base) if base is not None else "n/a",
                    _format_seconds(delta) if "duration" in key or "time" in key or "io_p" in key else f"{delta * 100:.2f}%" if delta is not None else "n/a",
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
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid #e3e7f0;
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
    .chart-label {{
      font-size: 10px;
      fill: var(--muted);
    }}
    .chart-value {{
      font-size: 10px;
      fill: var(--text);
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>ETL UX & Performance Report</h1>
    <div class="subtitle">Generated {_html_escape(metrics.get("metadata", {}).get("generated_at", ""))} • {_html_escape(metrics.get("metadata", {}).get("etl_path", ""))}</div>

    <div class="cards">
      <div class="card"><h3>Trace duration</h3><div class="value">{_format_seconds(duration_s)}</div></div>
      <div class="card"><h3>Slow I/O time</h3><div class="value">{_format_seconds(io.get("slow_time_s"))}</div></div>
      <div class="card"><h3>Slow I/O %</h3><div class="value">{slow_pct:.2f}%</div></div>
      <div class="card"><h3>I/O p95</h3><div class="value">{_format_seconds(io_percentiles.get("p95_s"))}</div></div>
    </div>

    {f'<div class="section"><h2>Comparison to baseline</h2>{_render_table(["Metric", "Current", "Baseline", "Delta", "Delta %"], comparison_rows)}</div>' if comparison else ''}

    <div class="section">
      <h2>Boot timeline</h2>
      {_render_timeline(boot)}
    </div>

    <div class="section">
      <h2>App launch latency (proxy)</h2>
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Windows WPR .etl files.")
    parser.add_argument("etl_path", help="Path to the .etl file")
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
    parser.add_argument("--compare", help="Compare against a baseline metrics JSON file")
    parser.add_argument("--slow-io-ms", type=int, default=50, help="Slow I/O threshold in ms")
    parser.add_argument("--top-n", type=int, default=10, help="Top N rows in report sections")
    parser.add_argument(
        "--ux-progress-every",
        type=int,
        default=200000,
        help="Log UX analysis progress every N events (debug only)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.no_etl_observer and args.force_etl_observer:
        print(
            "Cannot use --no-etl-observer with --force-etl-observer.",
            file=sys.stderr,
        )
        return 1

    if not os.path.isfile(args.etl_path):
        print(f"ETL file not found: {args.etl_path}", file=sys.stderr)
        return 1

    timestamp_scale = _read_perf_counter_scale(args.etl_path)

    ux_required = bool(args.report or args.metrics_output or args.compare)
    if ux_required:
        metrics_path = args.metrics_output
        if not metrics_path:
            if args.report:
                report_dir = os.path.dirname(args.report) or "."
                metrics_path = os.path.join(report_dir, "etl_metrics.json")
            else:
                metrics_path = "etl_metrics.json"

        ux = ETLUXAnalyzer(
            args.etl_path,
            slow_io_ms=args.slow_io_ms,
            top_n=args.top_n,
            debug=args.debug,
            progress_every=args.ux_progress_every,
            timestamp_scale=timestamp_scale,
        )
        try:
            metrics = ux.analyze()
        except OSError as exc:
            print(f"Failed to read ETL file: {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:  # pragma: no cover - unexpected
            print(f"Unhandled error: {exc}", file=sys.stderr)
            return 1

        if metrics_path:
            with open(metrics_path, "w", encoding="utf-8") as handle:
                json.dump(metrics, handle, indent=2)

        baseline = None
        if args.compare:
            try:
                with open(args.compare, "r", encoding="utf-8") as handle:
                    baseline = json.load(handle)
            except OSError as exc:
                print(f"Failed to read baseline metrics: {exc}", file=sys.stderr)
                return 1

        if args.report:
            report_html = _render_report(metrics, baseline)
            with open(args.report, "w", encoding="utf-8") as handle:
                handle.write(report_html)

        return 0

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
