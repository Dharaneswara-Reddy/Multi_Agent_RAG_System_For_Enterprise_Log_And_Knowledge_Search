"""Per-claim citation verification.

`check_output` verifies that citations *exist* and are not fabricated. That is
necessary and not sufficient: an answer can cite three real sources and still
invent the number in the middle of a sentence. Citing correctly and stating
something the sources do not say is the failure mode that survives a
document-level grounding check, and it is the one that gets an on-call engineer
to run the wrong command.

This module verifies claims at the level where fabrication actually shows up:
the **checkable atom**. Error codes, CLI flags, numeric thresholds, config keys,
file paths and commands are exact strings. If an answer says
`--batch-size=50000` and that string appears nowhere in the retrieved context,
the model produced it — from its training data, from a different runbook, or
from nothing. Any of those is a fabrication with respect to *this* corpus.

Why atoms rather than sentence-level entailment:

- It is **deterministic**, so it runs in CI with no API key and no second model
  to trust. An LLM judge grading groundedness is itself ungrounded.
- It is **high precision**. Prose paraphrases legitimately; `PAY-5021` does not.
  Flagging a rephrased sentence would be noise, and a noisy guardrail gets
  switched off. Flagging an invented flag value is always worth a human's time.
- It **fails in the safe direction**. It cannot catch every hallucination — a
  fabricated causal claim in plain prose passes — so it is a floor, not a
  ceiling, and the composite confidence score is what carries the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Atoms worth checking: strings a model cannot legitimately paraphrase.
_ATOM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # PAY-5021, ADR-0052, INC-2026-0714-01
    ("identifier", re.compile(r"\b[A-Z]{2,6}-\d{4}(?:-\d{2})?\b")),
    # --batch-size=50000, --window=2h
    ("flag", re.compile(r"--[a-z][a-z0-9-]*(?:=[^\s`'\"]+)?")),
    # cart.anon_ttl_hours=24, payments.breaker.enabled=true
    ("config_key", re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,4}\s*=\s*[^\s`'\"]+")),
    # bin/reconcile-charges, kubectl, pg_terminate_backend
    ("command", re.compile(r"\b(?:bin|scripts)/[a-z][a-z0-9-]*\b|\bpg_[a-z_]+\b")),
    # 4GiB, 3000ms, 250ms, 99.99%, 0.9
    ("quantity", re.compile(r"\b\d+(?:\.\d+)?\s*(?:GiB|MiB|GB|MB|ms|s|%|/min|/s)\b")),
)

# Sentence split that does not break on "e.g." or version numbers.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`])")

# Hedged statements are not claims about the corpus, so their atoms are not
# fabrications — the answer is explicitly not asserting them as fact.
_HEDGE_RE = re.compile(
    r"\b(?:may|might|could|possibly|typically|generally|usually|for example|e\.g\.|"
    r"not documented|no documentation|not covered|unclear|unknown)\b",
    re.I,
)


@dataclass
class ClaimCheck:
    """Result of verifying one answer against its context."""

    total_atoms: int = 0
    supported_atoms: int = 0
    unsupported: list[tuple[str, str]] = field(default_factory=list)  # (kind, atom)
    unsupported_sentences: list[str] = field(default_factory=list)

    @property
    def support_ratio(self) -> float:
        """Fraction of checkable atoms found in context. 1.0 when nothing to check.

        An answer with no checkable atoms is not thereby suspicious — "escalate
        to the Payments on-call" is a perfectly good grounded answer containing
        nothing exact. Returning 1.0 keeps the metric from punishing prose.
        """
        if self.total_atoms == 0:
            return 1.0
        return self.supported_atoms / self.total_atoms

    @property
    def has_fabrication(self) -> bool:
        return bool(self.unsupported)


def _normalise(text: str) -> str:
    """Fold whitespace and case so trivial formatting differences do not read as fabrication."""
    return re.sub(r"\s+", " ", text).lower()


def extract_atoms(text: str) -> list[tuple[str, str]]:
    """Checkable atoms in `text`, as (kind, atom) with duplicates removed."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in _ATOM_PATTERNS:
        for match in pattern.findall(text):
            atom = match.strip().rstrip(".,;:)")
            key = _normalise(atom)
            if key and key not in seen:
                seen.add(key)
                found.append((kind, atom))
    return found


def verify_claims(answer: str, context_text: str) -> ClaimCheck:
    """Check every atom in `answer` against `context_text`.

    Config keys are compared on their key half only: an answer that recommends
    `cart.anon_ttl_hours=24` where the runbook documents the same setting at a
    different value is making a judgement, not inventing a setting, and the
    value is the responder's decision to make.
    """
    result = ClaimCheck()
    if not answer.strip():
        return result

    haystack = _normalise(context_text)

    for sentence in _SENTENCE_RE.split(answer):
        if _HEDGE_RE.search(sentence):
            continue
        sentence_unsupported = False
        for kind, atom in extract_atoms(sentence):
            result.total_atoms += 1
            needle = _normalise(atom)
            if kind == "config_key":
                needle = needle.split("=")[0].strip()
            if needle in haystack:
                result.supported_atoms += 1
            else:
                result.unsupported.append((kind, atom))
                sentence_unsupported = True
        if sentence_unsupported:
            cleaned = sentence.strip().replace("\n", " ")
            result.unsupported_sentences.append(cleaned[:160])

    return result
