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
    # Per-claim verification: the fraction of checkable atoms (error codes, CLI
    # flags, thresholds) in the answer that actually appear in the context, and
    # the ones that did not. 1.0 when the answer contains nothing exact.
    claim_support: float = 1.0
    unsupported_claims: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity is Severity.BLOCK for f in self.findings)


# --------------------------------------------------------------------------
# 1. Input: PII redaction
# --------------------------------------------------------------------------

PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # Quadratic on adversarial input, deliberately left as-is: the bound lives
    # in check_input, which truncates before scanning. The cost is not the
    # domain-side ambiguity but the scan itself — at every start position
    # `[\w.%-]+` consumes a long run of word characters looking for an '@' that
    # never comes, which is O(n) work at O(n) positions. Rewriting the domain
    # as `[\w-]+(?:\.[\w-]+)+` was measured at 1140ms against the original's
    # 1138ms on 32k chars, and a fully possessive variant at 1804ms — no
    # rewrite of this pattern fixes it, so do not "optimise" it and assume the
    # cap is redundant. Bounded input is the only thing keeping this cheap.
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
    # Truncate first, scan second. The order is the whole point.
    #
    # This previously scanned the full untruncated input and applied the 8000
    # cap afterwards, which made the cap decorative: the expensive work had
    # already happened. `redact_pii`'s email pattern is quadratic in the input
    # length — `[\w.%-]+` and `[\w.-]+` both admit '.', so a string of "a.a.a…"
    # gives the engine an ambiguous split to backtrack over. Measured on this
    # corpus, cost grew 4x per doubling: 8k chars 73ms, 32k chars 1.14s, and a
    # 200KB paste is roughly a hundred seconds of CPU.
    #
    # That was reachable by any anonymous visitor through the public question
    # box, against a single 0.5 vCPU task, holding the GIL — enough to wedge
    # the console for everyone and eventually fail the ALB health check. The
    # FastAPI surface was never exposed to it, because AskRequest already
    # bounds the field at 8000; only the Streamlit path lacked the guard.
    findings: list[GuardrailFinding] = []
    if len(text) > 8000:
        findings.append(
            GuardrailFinding("oversized_query", Severity.WARN, f"{len(text)} chars, truncating")
        )
        text = text[:8000]

    findings.extend(detect_injection(text))
    redacted, pii_findings, count = redact_pii(text)
    findings.extend(pii_findings)
    if len(text.strip()) < 3:
        findings.append(GuardrailFinding("empty_query", Severity.BLOCK, "query too short"))
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


def check_output(
    answer: str, allowed_refs: list[str], context_text: str | None = None
) -> OutputCheck:
    """Validate a generated answer against the context it was given.

    When `context_text` is supplied, verification goes beyond "are the citations
    real" to "does the context actually contain the exact things this answer
    asserts" — see `guardrails.verify`.
    """
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

    claim_support = 1.0
    unsupported_claims: list[str] = []
    if context_text:
        from aiops.guardrails.verify import verify_claims

        check = verify_claims(answer, context_text)
        claim_support = check.support_ratio
        unsupported_claims = [atom for _kind, atom in check.unsupported]
        if check.has_fabrication:
            # WARN rather than BLOCK: the atom extractor is high-precision but
            # not perfect, and blocking an answer on a formatting difference
            # would train people to route around the guardrail. It costs
            # confidence, which routes the answer to a human instead.
            findings.append(
                GuardrailFinding(
                    "unsupported_claim",
                    Severity.WARN,
                    "answer states specifics absent from context: "
                    + ", ".join(unsupported_claims[:4]),
                )
            )

    grounded = bool(cited) and not unknown
    return OutputCheck(
        findings=findings,
        cited_refs=sorted(set(cited)),
        unknown_refs=unknown,
        grounded=grounded,
        claim_support=claim_support,
        unsupported_claims=unsupported_claims,
    )


# --------------------------------------------------------------------------
# 3. Escalation policy
# --------------------------------------------------------------------------

# Measured by scripts/calibrate.py against the **full production pipeline**
# (multi-query -> fuse -> rerank -> hop), not against bare index search.
# Off-corpus questions peak at 0.597; on-corpus questions bottom out at 0.619 —
# a separation of 0.022.
#
# The band has narrowed twice, and both moves are real rather than regressions:
#
#   18-doc corpus, 12 probes, single-query : 0.153
#   219-doc corpus, 24 probes, single-query: 0.048
#   219-doc corpus, 24 probes, full pipeline: 0.022
#
# The first narrowing was mostly corpus growth plus an honest probe set — the
# original probes only asked about the best-covered services. The second is a
# direct consequence of better retrieval: a pipeline that tries four query
# formulations and reranks finds the best available match for *any* question,
# including one the corpus cannot answer. Retrieval quality and
# out-of-scope detectability pull against each other, and this is what that
# trade-off looks like when it is measured instead of assumed.
#
# The floor therefore had to move *up* to 0.61 (0.58 now sits below the 0.597
# off-corpus peak, which is exactly how "who won the 1998 world cup" started
# getting answered instead of escalated).
#
# A 0.022 margin is thin enough that the cosine term alone should not be
# trusted to carry escalation, which is why confidence composes corroboration,
# citation grounding, and per-claim verification alongside it. Re-run the
# calibration after *any* retrieval change, not just an embedding or corpus
# change — the pipeline is now part of what these constants describe.
RELEVANCE_FLOOR = 0.61
RELEVANCE_CEIL = 0.84


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

    # Per-claim verification enters as a multiplier rather than a fourth
    # weighted term. An answer that cites correctly and then states a threshold
    # the sources never mention should not be rescued by strong retrieval and
    # good corroboration — those are exactly the conditions under which a
    # fabricated specific is most believable. Floored at 0.4 so a single
    # unmatched atom degrades toward escalation instead of zeroing the score.
    support = max(0.4, output.claim_support)

    return round(
        (0.45 * retrieval + 0.25 * corroboration + 0.30 * grounding) * penalty * support, 4
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
