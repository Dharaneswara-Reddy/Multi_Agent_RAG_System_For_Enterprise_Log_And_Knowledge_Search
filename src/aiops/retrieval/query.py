"""Query rewriting and multi-query retrieval.

A single embedding of the user's question is one point in vector space, and the
question's phrasing decides where that point lands. "Why do carts keep
vanishing?" and "Redis eviction on the cart cluster" describe the same incident
and embed to different neighbourhoods — the first matches nothing in a corpus
written by engineers.

Multi-query retrieval issues several formulations and fuses the result lists.
It is strictly more robust than picking one formulation and hoping, and it
costs nothing but retrieval time because the variants are cheap to construct.

**The variants are deterministic by default.** An LLM can generate better
paraphrases, but the eval harness must run in CI without an API key, and a
retrieval feature that only exists when a key is present is a feature that
never gets measured. The deterministic variants below target the specific
failure modes of this corpus:

- an **identifier query**, because error codes are the single most searchable
  strings here and a conversational question buries them in stopwords;
- a **keyword query**, which strips filler that drags the embedding toward
  generic phrasing;
- an **imperative rewrite**, because the corpus is written as instructions
  ("Check the status page") while questions are asked as questions.

When a model is available, `llm_variants` adds paraphrases on top.
"""

from __future__ import annotations

import re

from aiops.config import settings

# Deliberately small: removing domain words would defeat the purpose. This is
# filler that appears in questions and essentially never in runbook prose.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "doing", "have", "has", "had", "having",
    "i", "we", "you", "they", "it", "this", "that", "these", "those",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "can", "could", "should", "would", "shall", "will", "may", "might", "must",
    "of", "in", "on", "at", "to", "for", "with", "from", "by", "about", "as",
    "if", "then", "than", "so", "and", "or", "but", "not", "no", "nor",
    "my", "our", "your", "their", "its", "please", "help", "me", "just",
    "really", "actually", "now", "currently", "going", "get", "got",
})

# Alternation order matters: the generic code shape `[A-Z]{2,6}-\d{4}` also
# matches the head of `INC-2025-1007-01`, so the longest pattern must come
# first or an incident id is silently truncated to `INC-2025`.
_IDENTIFIER_RE = re.compile(
    r"\b(?:INC-\d{4}-\d{4}(?:-\d{2})?|ADR-\d{4}|[A-Z]{2,6}-\d{4})\b"
)

# Questions open with an interrogative *and* usually an auxiliary and a pronoun
# ("How do I fix..."). Stripping only the first word leaves "do I fix", which
# still embeds as a question rather than as the instruction the corpus contains.
_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(?:question:\s*)?"
    r"(?:what|why|how|when|where|which|who|whom|can|could|should|would|do|does|did|is|are|was|were)\b"
    r"(?:\s+(?:do|does|did|should|can|could|would|will|is|are|was|were|to))?"
    r"(?:\s+(?:i|we|you|they|it|one))?"
    r"[^\w]*",
    re.I,
)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]*", text)


def identifier_query(question: str) -> str | None:
    """Just the error codes / ADR ids / incident ids, if any.

    BM25 scores this near-perfectly against the runbook that owns the code,
    which is exactly the signal a conversational phrasing dilutes.
    """
    found = _IDENTIFIER_RE.findall(question.upper())
    return " ".join(dict.fromkeys(found)) if found else None


def keyword_query(question: str) -> str | None:
    """Content words only."""
    kept = [t for t in _tokens(question) if t.lower() not in _STOPWORDS and len(t) > 2]
    if len(kept) < 2:
        return None
    out = " ".join(kept)
    return out if out.lower() != question.lower().strip("? ") else None


def imperative_query(question: str) -> str | None:
    """Strip the interrogative opening so the text reads like the corpus does."""
    stripped = _QUESTION_PREFIX_RE.sub("", question).strip(" ?.")
    if len(stripped) < 8 or stripped.lower() == question.lower().strip(" ?."):
        return None
    return stripped


def deterministic_variants(question: str, limit: int | None = None) -> list[str]:
    """Query formulations derived without a model. Always includes the original.

    Order matters: the original is first so that when fusion ties, the user's
    actual phrasing wins.
    """
    limit = settings.multiquery_max_variants if limit is None else limit
    variants = [question.strip()]
    for candidate in (identifier_query(question), keyword_query(question), imperative_query(question)):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants[:limit]


def llm_variants(question: str, n: int = 2) -> list[str]:
    """Model-generated paraphrases, appended to the deterministic set.

    Returns the deterministic variants unchanged when offline or on any error —
    query expansion must never be able to fail a request. A retrieval
    enhancement that can take down retrieval is a liability, not a feature.
    """
    base = deterministic_variants(question)
    if not settings.multiquery_use_llm:
        return base
    try:
        from aiops.llm import complete_json
        from aiops.offline import is_offline

        if is_offline():
            return base
        schema = {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"}, "maxItems": n}
            },
            "required": ["queries"],
        }
        data = complete_json(
            system=(
                "Rewrite the user's question as short search queries for a corpus of SRE "
                "runbooks, ADRs, post-mortems and service documentation. Use the vocabulary "
                "an engineer would write in a runbook, not conversational phrasing. Preserve "
                "any error codes exactly. Return only the queries."
            ),
            prompt=question,
            schema=schema,
            model=settings.cheap_model,
        )
        extra = [q.strip() for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
        for q in extra:
            if q not in base:
                base.append(q)
    except Exception:
        return base
    return base[: settings.multiquery_max_variants]
