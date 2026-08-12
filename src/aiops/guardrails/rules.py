"""Guardrails: input validation, output validation, and the escalation decision.

Deliberately deterministic. Every rule here is a regex or a set operation, no
model call — which means guardrails cannot themselves hallucinate, cost nothing,
add no latency, and behave identically in tests and production. An LLM judge is
useful for *quality* scoring (see `evaluation.judge`); it is the wrong tool for a
safety gate that must be auditable.

Three layers:

1. **Input** — PII redaction and prompt-injection detection before text reaches
   a model or the audit log.
2. **Output** — citation grounding, unsupported-claim detection, and destructive
   -action flagging on the generated answer.
3. **Escalation** — a confidence policy deciding whether the answer is served or
   routed to a human review queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


@dataclass
class GuardrailFinding:
    rule: str
    severity: Severity
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.rule}: {self.detail}"


@dataclass
class InputCheck:
    text: str  # redacted text — this is what downstream sees
    findings: list[GuardrailFinding] = field(default_factory=list)
    redactions: int = 0

    @property
    def blocked(self) -> bool:
        return any(f.severity is Severity.BLOCK for f in self.findings)


@dataclass
class OutputCheck:
    findings: list[GuardrailFinding] = field(default_factory=list)
    cited_refs: list[str] = field(default_factory=list)
    unknown_refs: list[str] = field(default_factory=list)
    grounded: bool = True

    @property
    def blocked(self) -> bool:
        return any(f.severity is Severity.BLOCK for f in self.findings)


# --------------------------------------------------------------------------
# 1. Input: PII redaction
# --------------------------------------------------------------------------

PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    # 13-19 digits with optional separators — card-shaped. Validated by Luhn below.
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_CARD]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    ("phone", re.compile(r"\b(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)|\d{3})[ -]\d{3}[ -]\d{4}\b"), "[REDACTED_PHONE]"),
    ("api_key", re.compile(r"\b(?:sk|pk|api|key|token)[-_][A-Za-z0-9_-]{16,}\b", re.IGNORECASE), "[REDACTED_SECRET]"),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}", re.IGNORECASE), "[REDACTED_BEARER]"),
    ("private_ip", re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "[REDACTED_IP]"),
]


def _luhn_ok(digits: str) -> bool:
    """Card-shaped strings are common in logs (order ids, trace fragments).
    Luhn keeps the redactor from mangling every long number it sees."""
    digits = re.sub(r"\D", "", digits)
    if not 13 <= len(digits) <= 19:
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact_pii(text: str) -> tuple[str, list[GuardrailFinding], int]:
    findings: list[GuardrailFinding] = []
    count = 0
    for name, pattern, replacement in PII_PATTERNS:
        if name == "credit_card":
            # `replacement` is bound as a default argument rather than captured,
            # so the closure cannot pick up a later loop iteration's value.
            def _sub(m: re.Match[str], _replacement: str = replacement) -> str:
                nonlocal count
                if _luhn_ok(m.group(0)):
                    count += 1
                    return _replacement
                return m.group(0)

            new_text = pattern.sub(_sub, text)
        else:
            new_text, n = pattern.subn(replacement, text)
            count += n
        if new_text != text:
            findings.append(
                GuardrailFinding("pii_redaction", Severity.INFO, f"redacted {name}")
            )
            text = new_text
    return text, findings, count


# --------------------------------------------------------------------------
# 1b. Input: prompt injection
# --------------------------------------------------------------------------

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction_override", re.compile(r"\bignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)\b", re.IGNORECASE)),
    ("role_hijack", re.compile(r"\b(you\s+are\s+now|from\s+now\s+on\s+you|act\s+as\s+(?:a\s+)?(?:dan|jailbroken))\b", re.IGNORECASE)),
    ("system_prompt_exfil", re.compile(r"\b(reveal|print|show|repeat|output)\s+(your\s+)?(system\s+prompt|instructions|initial\s+prompt)\b", re.IGNORECASE)),
    ("delimiter_injection", re.compile(r"</?(system|assistant|human)>|\[/?INST\]", re.IGNORECASE)),
    ("guardrail_disable", re.compile(r"\b(disable|bypass|turn\s+off|skip)\s+(your\s+)?(safety|guardrails?|filters?|restrictions?)\b", re.IGNORECASE)),
    # Credential requests are blocked, not escalated. Escalation queues a draft
    # answer for a human to approve; for a secrets request there is no draft
    # worth writing, and the retrieval step should not run at all.
    (
        "exfil_request",
        re.compile(
            r"\b(?:list|dump|show|give|tell|reveal|print|fetch|retrieve"
            r"|what(?:'s|\s+is|\s+are)|where(?:'s|\s+is|\s+are))\b"
            r"[^.?!\n]{0,48}?"
            r"\b(?:api[\s_-]?keys?|passwords?|passphrases?|secrets?|credentials?"
            r"|access[\s_-]?keys?|private[\s_-]?keys?|auth[\s_-]?tokens?"
            r"|env(?:ironment)?\s+variables?)\b",
            re.IGNORECASE,
        ),
    ),
]


def detect_injection(text: str) -> list[GuardrailFinding]:
    out: list[GuardrailFinding] = []
    for name, pattern in INJECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            out.append(
                GuardrailFinding(
                    "prompt_injection",
                    Severity.BLOCK,
                    f"{name} pattern matched: {m.group(0)[:60]!r}",
                )
            )
    return out


def check_input(text: str) -> InputCheck:
    findings = detect_injection(text)
    redacted, pii_findings, count = redact_pii(text)
    findings.extend(pii_findings)
    if len(text.strip()) < 3:
        findings.append(GuardrailFinding("empty_query", Severity.BLOCK, "query too short"))
    if len(text) > 8000:
        findings.append(
            GuardrailFinding("oversized_query", Severity.WARN, f"{len(text)} chars, truncating")
        )
        redacted = redacted[:8000]
    return InputCheck(text=redacted, findings=findings, redactions=count)


# --------------------------------------------------------------------------
# 2. Output: grounding and safety
# --------------------------------------------------------------------------

CITATION_RE = re.compile(r"\[([^\[\]]+?#\d+)\]")

DESTRUCTIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("data_deletion", re.compile(r"\b(drop\s+table|truncate\s+table|delete\s+from|rm\s+-rf|dd\s+if=)\b", re.IGNORECASE)),
    ("cluster_mutation", re.compile(r"\bkubectl\s+delete\b|\bhelm\s+uninstall\b|\bterraform\s+destroy\b", re.IGNORECASE)),
    ("process_kill", re.compile(r"\bpg_terminate_backend\b|\bkill\s+-9\b", re.IGNORECASE)),
    ("financial_action", re.compile(r"\b(refund|charge|capture|void)\s+(all|every|the\s+customer)", re.IGNORECASE)),
]

HEDGE_RE = re.compile(
    r"\b(i (?:think|believe|assume)|probably|might be|possibly|it seems|not sure|unclear)\b", re.IGNORECASE
)


def check_output(answer: str, allowed_refs: list[str]) -> OutputCheck:
    """Validate a generated answer against the context it was given."""
    findings: list[GuardrailFinding] = []
    allowed = set(allowed_refs)

    cited = CITATION_RE.findall(answer)
    unknown = sorted({c for c in cited if c not in allowed})

    if allowed and not cited:
        findings.append(
            GuardrailFinding(
                "missing_citations",
                Severity.WARN,
                "answer cites no sources despite context being supplied",
            )
        )
    if unknown:
        # A citation to a ref that was never in context is a fabricated source —
        # the single most damaging failure mode for a system like this.
        findings.append(
            GuardrailFinding(
                "fabricated_citation",
                Severity.BLOCK,
                f"cites sources not in context: {', '.join(unknown[:4])}",
            )
        )

    for name, pattern in DESTRUCTIVE_PATTERNS:
        m = pattern.search(answer)
        if m:
            findings.append(
                GuardrailFinding(
                    "destructive_action",
                    Severity.WARN,
                    f"{name}: recommends {m.group(0)!r} — requires human approval",
                )
            )

    grounded = bool(cited) and not unknown
    return OutputCheck(
        findings=findings,
        cited_refs=sorted(set(cited)),
        unknown_refs=unknown,
        grounded=grounded,
    )


# --------------------------------------------------------------------------
# 3. Escalation policy
# --------------------------------------------------------------------------

# Measured on this corpus + bge-small-en-v1.5 by scripts/calibrate.py, at the
# tuned chunk size (320 tokens). Off-corpus questions peak at 0.554; on-corpus
# questions bottom out at 0.602 — a separation of 0.048.
#
# That separation is much narrower than the 0.153 measured on the 18-document
# corpus, and the honest reading is that the old figure was flattering rather
# than that anything regressed. Two things changed together: the corpus grew to
# 219 documents, and the probe set grew from 12 questions covering seven
# services to 24 covering all 42. The old probes only ever asked about the
# corpus's densest, best-covered region.
#
# The practical consequence is that the floor had to come *down*, from 0.64 to
# 0.58. At 0.64 the floor now sits above the on-corpus minimum of 0.602, so
# legitimate questions about thinly-covered services would score as
# low-relevance and over-escalate. A narrower band is a real cost of scale: the
# more the corpus contains, the less a single cosine score distinguishes
# "covered" from "not covered", which is the argument for the corroboration and
# grounding terms carrying weight alongside it.
RELEVANCE_FLOOR = 0.58
RELEVANCE_CEIL = 0.85


@dataclass
class EscalationDecision:
    escalate: bool
    reason: str = ""
    confidence: float = 0.0


def score_confidence(
    *,
    top_dense: float,
    distinct_sources: int,
    output: OutputCheck,
    context_empty: bool,
) -> float:
    """A transparent confidence heuristic, not a model self-report.

    Models are poorly calibrated when asked "how confident are you", and a
    self-reported number cannot be audited. This composes signals that are each
    independently checkable: retrieval strength, corroboration across sources,
    and whether the answer actually grounded itself in what it was given.

    `top_dense` must be the **raw cosine** of the best hit, not the blended rank
    score. Blended scores are min-max normalised per query, so the top hit is
    always exactly 1.0 and carries no information about whether retrieval
    actually found anything relevant.

    The rescale band is measured, not guessed. Over this corpus with
    bge-small-en-v1.5 (`scripts/calibrate.py`), the top-hit cosine for
    on-corpus questions ranges 0.719-0.872 while deliberately off-corpus
    questions ("who won the 1998 world cup") top out at 0.566. The band below
    maps that gap onto 0..1, and `RELEVANCE_FLOOR` sits inside it: below the
    floor the corpus simply does not contain the answer, and no amount of
    fluent citation should produce a confident response.

    Re-run the calibration whenever the embedding model or corpus changes —
    these constants are properties of that pair, not universal truths.
    """
    if context_empty:
        return 0.0
    if top_dense < RELEVANCE_FLOOR:
        # Nothing relevant was retrieved. Cap hard rather than letting
        # corroboration and grounding carry a confident-looking score.
        return round(min(0.25, 0.25 * (top_dense / RELEVANCE_FLOOR)), 4)
    retrieval = min(1.0, max(0.0, (top_dense - RELEVANCE_FLOOR) / (RELEVANCE_CEIL - RELEVANCE_FLOOR)))
    corroboration = min(1.0, distinct_sources / 3.0)
    grounding = 1.0 if output.grounded else 0.35
    penalty = 0.5 if output.blocked else 1.0
    return round(
        (0.45 * retrieval + 0.25 * corroboration + 0.30 * grounding) * penalty, 4
    )


def decide_escalation(
    confidence: float,
    output: OutputCheck,
    *,
    threshold: float | None = None,
    distinct_sources: int = 0,
) -> EscalationDecision:
    from aiops.config import settings

    threshold = settings.confidence_threshold if threshold is None else threshold

    if output.blocked:
        return EscalationDecision(True, "output guardrail blocked the answer", confidence)
    if confidence < threshold:
        return EscalationDecision(
            True, f"confidence {confidence:.2f} below threshold {threshold:.2f}", confidence
        )
    if distinct_sources < 2 and any(
        f.rule == "destructive_action" for f in output.findings
    ):
        # The triage guide's rule: a destructive recommendation backed by a
        # single source is exactly the case a human should see first.
        return EscalationDecision(
            True, "destructive recommendation supported by fewer than two sources", confidence
        )
    return EscalationDecision(False, "", confidence)
