"""Parser tests, including the real-LogHub stress suite.

The stress test is the point: synthetic logs are shaped by the same author as
the parser, so they prove nothing about robustness. These assertions run against
unmodified capture files from nine production systems.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops.ingestion.parsers import (
    parse_file,
    parse_line,
    parse_lines,
    parse_stats,
)

EXTERNAL = Path(__file__).resolve().parents[1] / "data" / "logs" / "external"

# Files present in the repo, with the format each should be attributed to.
EXPECTED_FORMAT = {
    "Apache_2k.log": "apache",
    "HDFS_2k.log": "hdfs",
    "Hadoop_2k.log": "hadoop",
    "Linux_2k.log": "syslog",
    "Mac_2k.log": "syslog",
    "OpenStack_2k.log": "openstack",
    "Spark_2k.log": "spark",
    "Thunderbird_2k.log": "thunderbird",
    "Zookeeper_2k.log": "zookeeper",
}


def test_meridian_format_extracts_structured_fields():
    line = (
        "2026-07-14T09:02:03.120Z ERROR payment-service trace_id=a1b2c3d4e5f60718 "
        "error_code=PAY-5021 latency_ms=3120 processor call failed: read timed out"
    )
    p = parse_line(line)
    assert p.fully_parsed
    assert p.format_name == "meridian"
    r = p.record
    assert r.level == "ERROR"
    assert r.service == "payment-service"
    assert r.error_code == "PAY-5021"
    assert r.latency_ms == 3120
    assert r.trace_id == "a1b2c3d4e5f60718"
    # kv pairs are stripped from the human-readable message
    assert "trace_id=" not in r.message
    assert "read timed out" in r.message


def test_parser_never_raises_on_hostile_input():
    hostile = [
        "",
        "   ",
        "\x00\x01\x02 binary garbage",
        "{" * 5000,
        "2026-13-45T99:99:99.999Z NOTALEVEL svc",
        "…unicode ✓ ∆ 日本語 emoji 🚨",
        "a" * 20000,
    ]
    for line in hostile:
        p = parse_line(line)  # must not raise
        assert p.record is not None


def test_fallback_still_extracts_error_codes():
    p = parse_line("some|weird|delimited|format|PAY-5021|failed")
    assert not p.fully_parsed
    assert p.record.error_code == "PAY-5021"


@pytest.mark.parametrize("filename", sorted(EXPECTED_FORMAT))
def test_loghub_files_parse_completely(filename):
    """Every real-world file must reach full parse coverage.

    A drop here means a production log format regressed — which is exactly the
    failure a synthetic-only test suite would miss.
    """
    path = EXTERNAL / filename
    if not path.exists():
        pytest.skip(f"{filename} not downloaded — run scripts/fetch_loghub.sh")
    parsed = parse_file(path)
    stats = parse_stats(parsed)
    assert stats["total"] > 100
    assert stats["coverage"] == 1.0, f"{filename}: {stats}"
    assert stats["by_format"].get(EXPECTED_FORMAT[filename], 0) > 0


def test_zookeeper_nested_brackets_in_thread_name():
    """Regression: the thread field nests brackets and broke a non-greedy regex."""
    line = (
        "2015-07-29 17:41:44,747 - INFO  "
        "[QuorumPeer[myid=1]/0:0:0:0:0:0:0:0:2181:FastLeaderElection@774] "
        "- Notification time out: 3200"
    )
    p = parse_line(line)
    assert p.fully_parsed and p.format_name == "zookeeper"
    assert p.record.message == "Notification time out: 3200"


def test_openstack_request_id_becomes_trace_id():
    line = (
        "nova-api.log.1.2017-05-16_13:53:08 2017-05-16 00:00:00.008 25746 INFO "
        "nova.osapi_compute.wsgi.server [req-38101a0b-2096-447d-96ea-a692162415ae "
        "113d3a99c3da401fbd62cc2caa5b96d2] 10.11.10.1 \"GET /v2/servers HTTP/1.1\""
    )
    p = parse_line(line)
    assert p.fully_parsed
    assert p.record.trace_id == "38101a0b-2096-447d-96ea-a692162415ae"


def test_hdfs_block_id_becomes_correlation_key():
    line = (
        "081109 203615 148 INFO dfs.DataNode$PacketResponder: "
        "PacketResponder 1 for block blk_38865049064139660 terminating"
    )
    p = parse_line(line)
    assert p.fully_parsed
    assert p.record.trace_id == "blk_38865049064139660"


def test_parse_stats_shape():
    lines = [
        "2026-07-14T09:00:00.000Z INFO api-gateway trace_id=aabbccdd11223344 GET /v1/cart",
        "nonsense line with no format",
    ]
    stats = parse_stats(parse_lines(lines))
    assert stats["total"] == 2
    assert stats["fully_parsed"] == 1
    assert stats["coverage"] == 0.5
