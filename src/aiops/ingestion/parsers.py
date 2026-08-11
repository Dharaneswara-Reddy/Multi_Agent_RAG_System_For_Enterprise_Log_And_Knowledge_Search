"""Log parsing: unstructured text in, structured records out.

Two design decisions worth defending:

1. **A parser registry, not one regex.** Real platforms emit several formats at
   once (a Java service, an nginx sidecar, a Python consumer). We try each
   known format and fall back to a permissive extractor rather than dropping
   the line — an unparsed line still carries a searchable message.

2. **Never raise on bad input.** `parse_line` always returns a record. A log
   pipeline that crashes on a malformed line is a log pipeline that loses the
   most interesting line in the file, because malformed lines cluster around
   incidents.

The LogHub formats (HDFS, OpenStack, Spark, Zookeeper, Apache, Linux, Mac,
Hadoop, Thunderbird) are handled here specifically so the parser is exercised
against real-world messiness, not only the synthetic Meridian format.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

from aiops.schemas import LogRecord

LEVELS = {"DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL", "TRACE", "NOTICE"}

_LEVEL_NORMALISE = {
    "WARNING": "WARN",
    "CRITICAL": "FATAL",
    "NOTICE": "INFO",
    "TRACE": "DEBUG",
}

_DEFAULT_TS = datetime(1970, 1, 1)

# Error-code shapes: three-to-six uppercase letters, dash, digits (PAY-5021).
ERROR_CODE_RE = re.compile(r"\b([A-Z]{2,6}-\d{3,5})\b")
TRACE_KV_RE = re.compile(r"\btrace_id=([0-9a-fA-F]{8,})\b")
LATENCY_KV_RE = re.compile(r"\blatency_ms=(\d+)\b")
# OpenStack request ids look like req-38101a0b-2096-447d-96ea-a692162415ae
REQ_ID_RE = re.compile(r"\breq-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")
# HDFS block ids make a serviceable correlation key for that corpus
BLOCK_ID_RE = re.compile(r"\b(blk_-?\d+)\b")


@dataclass(frozen=True)
class ParsedLine:
    record: LogRecord
    format_name: str
    fully_parsed: bool


def _norm_level(raw: str | None) -> str:
    if not raw:
        return "INFO"
    lvl = raw.strip().upper()
    lvl = _LEVEL_NORMALISE.get(lvl, lvl)
    return lvl if lvl in {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"} else "INFO"


def _derive_trace(line: str) -> str:
    for rx in (TRACE_KV_RE, REQ_ID_RE, BLOCK_ID_RE):
        m = rx.search(line)
        if m:
            return m.group(1)
    return ""


# --------------------------------------------------------------------------
# Format handlers. Each returns a LogRecord or None.
# --------------------------------------------------------------------------

MERIDIAN_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\s+"
    r"(?P<level>[A-Z]+)\s+(?P<service>[a-z0-9\-]+)\s+(?P<rest>.*)$"
)


def _meridian(line: str) -> LogRecord | None:
    m = MERIDIAN_RE.match(line)
    if not m:
        return None
    rest = m.group("rest")
    code = ERROR_CODE_RE.search(rest)
    lat = LATENCY_KV_RE.search(rest)
    # Strip the structured kv pairs out of the human message.
    message = re.sub(r"\b(trace_id|error_code|latency_ms)=\S+\s*", "", rest).strip()
    return LogRecord(
        timestamp=datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%S.%fZ"),
        level=_norm_level(m.group("level")),
        service=m.group("service"),
        trace_id=_derive_trace(rest),
        message=message,
        error_code=code.group(1) if code else None,
        latency_ms=int(lat.group(1)) if lat else None,
        raw=line,
    )


HDFS_RE = re.compile(
    r"^(?P<date>\d{6})\s+(?P<time>\d{6})\s+(?P<pid>\d+)\s+(?P<level>[A-Z]+)\s+"
    r"(?P<component>[\w$.]+):\s*(?P<msg>.*)$"
)


def _hdfs(line: str) -> LogRecord | None:
    m = HDFS_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("date") + m.group("time"), "%y%m%d%H%M%S")
    except ValueError:
        ts = _DEFAULT_TS
    return LogRecord(
        timestamp=ts,
        level=_norm_level(m.group("level")),
        service="hdfs/" + m.group("component").split("$")[0].split(".")[-1],
        trace_id=_derive_trace(line),
        message=m.group("msg").strip(),
        raw=line,
    )


SPARK_RE = re.compile(
    r"^(?P<ts>\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+(?P<level>[A-Z]+)\s+"
    r"(?P<component>[\w$.]+):\s*(?P<msg>.*)$"
)


def _spark(line: str) -> LogRecord | None:
    m = SPARK_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%y/%m/%d %H:%M:%S")
    except ValueError:
        ts = _DEFAULT_TS
    return LogRecord(
        timestamp=ts,
        level=_norm_level(m.group("level")),
        service="spark/" + m.group("component").split(".")[0],
        trace_id=_derive_trace(line),
        message=m.group("msg").strip(),
        raw=line,
    )


# Hadoop: 2015-10-18 18:01:47,978 INFO [main] org.apache.…: message
HADOOP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d{3})\s+(?P<level>[A-Z]+)\s+"
    r"\[(?P<thread>[^\]]+)\]\s+(?P<component>[\w$.]+):\s*(?P<msg>.*)$"
)


def _hadoop(line: str) -> LogRecord | None:
    m = HADOOP_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        ts = _DEFAULT_TS
    return LogRecord(
        timestamp=ts,
        level=_norm_level(m.group("level")),
        service="hadoop/" + m.group("component").split(".")[-1],
        trace_id=_derive_trace(line),
        message=m.group("msg").strip(),
        raw=line,
    )


# Zookeeper: 2015-07-29 17:41:44,747 - INFO  [thread:Class@line] - message
# The thread field nests brackets (`QuorumPeer[myid=1]/0:0:...:FastLeaderElection@774`),
# so the group must be greedy and anchored on the trailing " - " separator rather
# than on the first closing bracket.
ZOOKEEPER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d{3})\s+-\s+(?P<level>[A-Z]+)\s+"
    r"\[(?P<thread>.+)\]\s+-\s*(?P<msg>.*)$"
)


def _zookeeper(line: str) -> LogRecord | None:
    m = ZOOKEEPER_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        ts = _DEFAULT_TS
    return LogRecord(
        timestamp=ts,
        level=_norm_level(m.group("level")),
        service="zookeeper",
        trace_id=_derive_trace(line),
        message=m.group("msg").strip(),
        raw=line,
    )


# OpenStack: <logfile> 2017-05-16 00:00:00.008 25746 INFO nova.x [req-… ] message
OPENSTACK_RE = re.compile(
    r"^(?P<file>\S+\.log\S*)\s+(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<level>[A-Z]+)\s+(?P<component>[\w.]+)\s*(?P<msg>.*)$"
)


def _openstack(line: str) -> LogRecord | None:
    m = OPENSTACK_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        ts = _DEFAULT_TS
    comp = m.group("component")
    return LogRecord(
        timestamp=ts,
        level=_norm_level(m.group("level")),
        service="openstack/" + comp.split(".")[0],
        trace_id=_derive_trace(line),
        message=m.group("msg").strip(),
        raw=line,
    )


# Apache: [Sun Dec 04 04:47:44 2005] [notice] message
APACHE_RE = re.compile(
    r"^\[(?P<ts>[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\]\s+"
    r"\[(?P<level>\w+)\]\s*(?P<msg>.*)$"
)


def _apache(line: str) -> LogRecord | None:
    m = APACHE_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        ts = _DEFAULT_TS
    return LogRecord(
        timestamp=ts,
        level=_norm_level(m.group("level")),
        service="apache-httpd",
        trace_id="",
        message=m.group("msg").strip(),
        raw=line,
    )


# Syslog (Linux / Mac): Jun 14 15:16:01 host proc[pid]: message
SYSLOG_RE = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<proc>[^\s:\[]+)(?:\[(?P<pid>\d+)\])?[^:]*:\s*(?P<msg>.*)$"
)


def _syslog(line: str) -> LogRecord | None:
    m = SYSLOG_RE.match(line)
    if not m:
        return None
    try:
        # Syslog omits the year; anchor to 2005 (the LogHub capture era) so
        # ordering within the file stays correct.
        ts = datetime.strptime("2005 " + m.group("ts"), "%Y %b %d %H:%M:%S")
    except ValueError:
        ts = _DEFAULT_TS
    msg = m.group("msg").strip()
    level = "ERROR" if re.search(r"\b(error|fail|denied|refused)\b", msg, re.IGNORECASE) else "INFO"
    return LogRecord(
        timestamp=ts,
        level=level,
        service=f"host/{m.group('proc')}",
        trace_id="",
        message=msg,
        raw=line,
    )


# Thunderbird: - 1131566461 2005.11.09 dn228 Nov 9 12:01:01 dn228/dn228 proc[pid]: msg
THUNDERBIRD_RE = re.compile(
    r"^(?P<flag>\S+)\s+(?P<epoch>\d{9,10})\s+(?P<date>\d{4}\.\d{2}\.\d{2})\s+(?P<node>\S+)\s+(?P<rest>.*)$"
)


def _thunderbird(line: str) -> LogRecord | None:
    m = THUNDERBIRD_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.fromtimestamp(int(m.group("epoch")))
    except (ValueError, OSError):
        ts = _DEFAULT_TS
    rest = m.group("rest")
    level = "ERROR" if m.group("flag") != "-" else "INFO"
    return LogRecord(
        timestamp=ts,
        level=level,
        service=f"thunderbird/{m.group('node')}",
        trace_id="",
        message=rest.strip(),
        raw=line,
    )


PARSERS: list[tuple[str, Callable[[str], LogRecord | None]]] = [
    ("meridian", _meridian),
    ("openstack", _openstack),
    ("hadoop", _hadoop),
    ("zookeeper", _zookeeper),
    ("spark", _spark),
    ("hdfs", _hdfs),
    ("apache", _apache),
    ("thunderbird", _thunderbird),
    ("syslog", _syslog),
]


def _fallback(line: str) -> LogRecord:
    """Best-effort extraction. Keeps the line searchable even when nothing matches."""
    level = "INFO"
    for token in LEVELS:
        if re.search(rf"\b{token}\b", line):
            level = _norm_level(token)
            break
    code = ERROR_CODE_RE.search(line)
    return LogRecord(
        timestamp=_DEFAULT_TS,
        level=level,
        service="unknown",
        trace_id=_derive_trace(line),
        message=line.strip()[:600],
        error_code=code.group(1) if code else None,
        raw=line,
    )


def parse_line(line: str) -> ParsedLine:
    """Parse one line. Always succeeds; `fully_parsed` says whether a format matched."""
    line = line.rstrip("\n")
    if not line.strip():
        return ParsedLine(_fallback(""), "empty", False)
    for name, fn in PARSERS:
        try:
            rec = fn(line)
        except Exception:  # a bad line must never take the pipeline down
            rec = None
        if rec is not None:
            return ParsedLine(rec, name, True)
    return ParsedLine(_fallback(line), "fallback", False)


def parse_lines(lines: Iterable[str]) -> Iterator[ParsedLine]:
    for line in lines:
        if line.strip():
            yield parse_line(line)


def parse_file(path) -> list[ParsedLine]:
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return list(parse_lines(text.splitlines()))


def parse_stats(parsed: Iterable[ParsedLine]) -> dict[str, object]:
    """Coverage report — the number to watch when a new log source is onboarded."""
    parsed = list(parsed)
    by_format: dict[str, int] = {}
    for p in parsed:
        by_format[p.format_name] = by_format.get(p.format_name, 0) + 1
    total = len(parsed)
    matched = sum(1 for p in parsed if p.fully_parsed)
    return {
        "total": total,
        "fully_parsed": matched,
        "coverage": round(matched / total, 4) if total else 0.0,
        "by_format": dict(sorted(by_format.items(), key=lambda kv: -kv[1])),
    }
