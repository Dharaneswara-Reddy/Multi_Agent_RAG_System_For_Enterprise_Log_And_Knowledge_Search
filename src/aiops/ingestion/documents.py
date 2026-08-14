"""Document loading and chunking.

Chunking is the highest-leverage context-engineering decision in the system, so
it is deliberately explicit rather than delegated to a library:

- **Markdown docs** are split on heading boundaries first, then packed to a
  token budget. A runbook section is a natural retrieval unit — splitting
  "## Remediation" in half produces two chunks that each answer nothing.
- **Every chunk carries its heading path** in the embedded text. Retrieval on
  "how do I fix the pool" should match a chunk whose body never repeats the word
  "inventory" but whose heading does.
- **Logs** are windowed by trace where possible, otherwise by time-ordered runs,
  because a single log line is rarely a useful retrieval unit while a burst is.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from aiops.config import settings
from aiops.ingestion.parsers import ParsedLine, parse_lines
from aiops.schemas import Chunk, SourceType

FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<yaml>.*?)\n---\s*\n", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# ~4 chars per token is close enough for budgeting and avoids a tokenizer
# dependency in the hot path. The eval harness is what validates the choice.
CHARS_PER_TOKEN = 4


def _tok(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _chunk_id(doc_id: str, ordinal: int, text: str) -> str:
    digest = hashlib.sha1(f"{doc_id}:{ordinal}:{text[:200]}".encode()).hexdigest()[:10]
    return f"{doc_id}::{ordinal:03d}::{digest}"


def _parse_frontmatter(raw: str) -> tuple[dict[str, object], str]:
    """Minimal YAML-ish frontmatter reader (scalars and inline lists only).

    Deliberately not a full YAML parser: the frontmatter schema here is fixed
    and small, and pulling in a parser for six keys is not worth the dependency.
    """
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    meta: dict[str, object] = {}
    for line in m.group("yaml").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("\"'") for v in value[1:-1].split(",")]
            meta[key] = [v for v in items if v]
        else:
            value = value.strip("\"'")
            meta[key] = None if value in {"null", "none", ""} else value
    return meta, raw[m.end() :]


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown into (heading_path, text) pairs, preserving hierarchy."""
    sections: list[tuple[str, str]] = []
    stack: list[str] = []
    current: list[str] = []
    current_path = ""

    def flush() -> None:
        text = "\n".join(current).strip()
        if text:
            sections.append((current_path, text))

    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            current = []
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: level - 1]
            stack.append(title)
            current_path = " > ".join(stack)
        else:
            current.append(line)
    flush()
    return sections


def _split_oversized(path: str, text: str, budget: int, overlap: int) -> list[tuple[str, str]]:
    """Split a section that exceeds the budget, with a sliding paragraph overlap
    so a fact spanning the boundary survives intact in at least one chunk."""
    out: list[tuple[str, str]] = []
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    cur: list[str] = []
    for para in paras:
        candidate = cur + [para]
        if _tok("\n\n".join(candidate)) > budget and cur:
            out.append((path, "\n\n".join(cur).strip()))
            tail = cur[-1] if _tok(cur[-1]) <= overlap else cur[-1][-overlap * CHARS_PER_TOKEN :]
            cur = [tail, para]
        else:
            cur = candidate
    if cur:
        out.append((path, "\n\n".join(cur).strip()))
    return out


def _pack(sections: list[tuple[str, str]], budget: int, overlap: int) -> list[tuple[str, str]]:
    """Turn sections into chunks.

    Policy: **one section, one chunk.** A markdown section in a runbook or ADR is
    an intentional semantic unit written by a human — "## Remediation" answers a
    question on its own, and merging it with "## Escalation" just to fill a token
    budget dilutes the embedding toward the average of two topics.

    Two exceptions:
    - A section over `budget` is split on paragraphs with overlap.
    - A run of *very* small sections (under `merge_below`) is merged, because a
      two-line stub embeds poorly on its own.

    `settings.doc_chunk_tokens` is therefore a ceiling, not a target. Both it and
    `merge_below` are swept in the eval harness.
    """
    merge_below = max(1, budget // 4)
    out: list[tuple[str, str]] = []
    buf_path, buf_text = "", ""

    def emit() -> None:
        nonlocal buf_path, buf_text
        if buf_text.strip():
            out.append((buf_path, buf_text.strip()))
        buf_path, buf_text = "", ""

    for path, text in sections:
        size = _tok(text)
        if size > budget:
            emit()
            out.extend(_split_oversized(path, text, budget, overlap))
            continue

        if size < merge_below and buf_text and _tok(buf_text) + size <= budget:
            buf_text = f"{buf_text}\n\n{text}"
            continue

        emit()
        if size < merge_below:
            buf_path, buf_text = path, text  # hold open in case the next one is tiny too
        else:
            out.append((path, text.strip()))
    emit()
    return out


def load_documents(docs_dir: Path | None = None) -> list[Chunk]:
    """Read every markdown file in `docs_dir` and return retrieval-ready chunks.

    When `AIOPS_DOCS_URI` names an S3 prefix and no explicit directory was
    passed, the corpus is synced down first: in a cloud deployment S3 is the
    source of truth, and the image ships code and models but not knowledge.
    An explicit `docs_dir` always wins, which is what keeps the tests, the local
    workflow and `scripts/setup.py` reading the filesystem unchanged.
    """
    explicit = docs_dir is not None
    docs_dir = docs_dir or settings.docs_dir

    if not explicit and settings.docs_uri:
        from aiops.storage.artifacts import fetch_documents

        fetch_documents(settings.docs_uri, Path(docs_dir))

    chunks: list[Chunk] = []

    for path in sorted(Path(docs_dir).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        doc_id = path.stem
        title = str(meta.get("title") or doc_id)
        try:
            source_type = SourceType(str(meta.get("source_type", "service_doc")))
        except ValueError:
            source_type = SourceType.SERVICE_DOC
        service = meta.get("service")
        service = str(service) if service else None
        codes = meta.get("error_codes") or []
        codes = [str(c) for c in codes] if isinstance(codes, list) else []

        packed = _pack(_split_sections(body), settings.doc_chunk_tokens, settings.doc_chunk_overlap)
        for ordinal, (heading_path, text) in enumerate(packed):
            # The heading path is prepended to the embedded text on purpose:
            # it is often the only place the subject noun appears.
            embed_text = f"[{title}] {heading_path}\n{text}" if heading_path else f"[{title}]\n{text}"
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(doc_id, ordinal, text),
                    doc_id=doc_id,
                    text=embed_text,
                    source_type=source_type,
                    title=title,
                    service=service,
                    error_codes=codes,
                    ordinal=ordinal,
                )
            )
    return chunks


def _window_key(rec) -> str:
    return rec.trace_id or ""


def chunk_logs(parsed: Iterable[ParsedLine], source_name: str) -> list[Chunk]:
    """Group parsed log lines into retrieval units.

    Lines sharing a trace id become one chunk regardless of how far apart they
    sit in the file — that is exactly the cross-service correlation an engineer
    reconstructs by hand during an incident, and it is what makes "why did
    checkout fail" retrievable in a single hit.
    """
    parsed = list(parsed)
    by_trace: dict[str, list[ParsedLine]] = {}
    untraced: list[ParsedLine] = []
    for p in parsed:
        key = _window_key(p.record)
        if key:
            by_trace.setdefault(key, []).append(p)
        else:
            untraced.append(p)

    chunks: list[Chunk] = []
    ordinal = 0

    max_chars = settings.max_log_chunk_chars

    def build(group: list[ParsedLine], trace_id: str | None) -> None:
        nonlocal ordinal
        if not group:
            return
        group.sort(key=lambda p: p.record.timestamp)
        lines = [p.record.raw or p.record.message for p in group]
        # A hot correlation key (an HDFS block touched hundreds of times) would
        # otherwise produce a chunk far larger than the embedding model's window.
        if sum(len(x) + 1 for x in lines) > max_chars:
            head: list[ParsedLine] = []
            size = 0
            for p, text in zip(group, lines, strict=True):
                if size + len(text) + 1 > max_chars and head:
                    build(head, trace_id)
                    head, size = [], 0
                head.append(p)
                size += len(text) + 1
            build(head, trace_id)
            return
        body = "\n".join(lines)
        services = sorted({p.record.service for p in group})
        codes = sorted({p.record.error_code for p in group if p.record.error_code})
        levels = sorted({p.record.level for p in group})
        header = (
            f"[log:{source_name}] services={','.join(services)} levels={','.join(levels)}"
            + (f" trace={trace_id}" if trace_id else "")
            + (f" error_codes={','.join(codes)}" if codes else "")
        )
        chunks.append(
            Chunk(
                chunk_id=_chunk_id(f"log-{source_name}", ordinal, body),
                doc_id=f"log-{source_name}",
                text=f"{header}\n{body}",
                source_type=SourceType.LOG,
                title=f"{source_name} logs",
                service=services[0] if len(services) == 1 else None,
                timestamp=group[0].record.timestamp,
                trace_id=trace_id,
                error_codes=codes,
                ordinal=ordinal,
            )
        )
        ordinal += 1

    for trace_id, group in by_trace.items():
        # A very hot trace id (an HDFS block touched hundreds of times) would
        # otherwise produce one enormous chunk; window those.
        for i in range(0, len(group), settings.log_window_size * 3):
            build(group[i : i + settings.log_window_size * 3], trace_id)

    untraced.sort(key=lambda p: p.record.timestamp)
    for i in range(0, len(untraced), settings.log_window_size):
        build(untraced[i : i + settings.log_window_size], None)

    return chunks


def load_logs(
    logs_dir: Path | None = None, external_limit: int = 400
) -> tuple[list[Chunk], dict[str, object]]:
    """Load the synthetic platform log plus a sample of real LogHub sources.

    `external_limit` caps lines per external file. The overlay exists to make
    retrieval face genuinely noisy neighbours; ingesting all 18k lines would
    slow index builds without changing what the overlay teaches us.
    """
    logs_dir = Path(logs_dir or settings.logs_dir)
    chunks: list[Chunk] = []
    stats: dict[str, object] = {}

    for path in sorted(logs_dir.glob("*.log")):
        parsed = list(parse_lines(path.read_text(encoding="utf-8", errors="replace").splitlines()))
        chunks.extend(chunk_logs(parsed, path.stem))
        stats[path.name] = len(parsed)

    external = logs_dir / "external"
    if external.is_dir():
        for path in sorted(external.glob("*.log")):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:external_limit]
            parsed = list(parse_lines(lines))
            chunks.extend(chunk_logs(parsed, f"external-{path.stem}"))
            stats[f"external/{path.name}"] = len(parsed)

    return chunks, stats


def build_all_chunks() -> tuple[list[Chunk], dict[str, object]]:
    docs = load_documents()
    logs, log_stats = load_logs()
    stats = {
        "doc_chunks": len(docs),
        "log_chunks": len(logs),
        "total_chunks": len(docs) + len(logs),
        "log_sources": log_stats,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    return docs + logs, stats
