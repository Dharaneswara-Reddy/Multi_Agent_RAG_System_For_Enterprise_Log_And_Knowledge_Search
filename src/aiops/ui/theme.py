"""Shared styling for the Streamlit UI.

Streamlit's defaults read as "internal tool". A small, consistent design system
— one accent, one surface colour, one border treatment, restrained type — is
most of the distance to something that looks deliberate. Everything here is
CSS variables so light and dark both work from one definition.
"""

from __future__ import annotations

# A single categorical palette used by every chart, so colour means the same
# thing on every screen.
PALETTE = {
    "answered": "#2E9E6B",
    "escalated": "#D68A1E",
    "blocked": "#C4462F",
    "accent": "#4C6EF5",
    "accent_soft": "#8DA2FB",
    "muted": "#8B93A7",
    "log": "#7C5CBF",
    "runbook": "#2E9E6B",
    "adr": "#4C6EF5",
    "postmortem": "#C4462F",
    "service_doc": "#0E9AA7",
}

SOURCE_ICON = {
    "runbook": "📕",
    "adr": "📐",
    "postmortem": "🔍",
    "service_doc": "📘",
    "log": "🧾",
}

CSS = """
<style>
:root {
  --aiops-accent: #4C6EF5;
  --aiops-accent-weak: rgba(76, 110, 245, 0.10);
  --aiops-ok: #2E9E6B;
  --aiops-warn: #D68A1E;
  --aiops-bad: #C4462F;
  --aiops-surface: rgba(140, 150, 175, 0.06);
  --aiops-border: rgba(140, 150, 175, 0.22);
  --aiops-muted: #8B93A7;
}

/* Tighten the default vertical rhythm — Streamlit is very generous by default */
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px; }

h1, h2, h3 { letter-spacing: -0.015em; font-weight: 640; }

/* ---- masthead ---- */
.aiops-head {
  display: flex; align-items: baseline; gap: 0.75rem;
  border-bottom: 1px solid var(--aiops-border);
  padding-bottom: 0.85rem; margin-bottom: 1.4rem;
}
.aiops-head h1 { margin: 0; font-size: 1.6rem; }
.aiops-head .sub { color: var(--aiops-muted); font-size: 0.88rem; }

/* ---- metric cards ---- */
.aiops-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.7rem; }
.aiops-card {
  border: 1px solid var(--aiops-border); border-radius: 12px;
  padding: 0.85rem 1rem; background: var(--aiops-surface);
}
.aiops-card .label {
  font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--aiops-muted); font-weight: 600;
}
.aiops-card .value { font-size: 1.5rem; font-weight: 660; line-height: 1.25; margin-top: 0.15rem; }
.aiops-card .foot { font-size: 0.74rem; color: var(--aiops-muted); }

/* ---- verdict pill ---- */
.pill {
  display: inline-block; padding: 0.18rem 0.62rem; border-radius: 999px;
  font-size: 0.74rem; font-weight: 660; letter-spacing: 0.02em;
}
.pill-answered  { background: rgba(46,158,107,.14); color: var(--aiops-ok); }
.pill-escalated { background: rgba(214,138,30,.15); color: var(--aiops-warn); }
.pill-blocked   { background: rgba(196,70,47,.14);  color: var(--aiops-bad); }
.pill-neutral   { background: var(--aiops-surface);  color: var(--aiops-muted); }

/* ---- answer surface ---- */
.aiops-answer {
  border: 1px solid var(--aiops-border); border-left: 3px solid var(--aiops-accent);
  border-radius: 10px; padding: 1.1rem 1.25rem; background: var(--aiops-surface);
}

/* ---- agent trace ---- */
.trace-step {
  display: flex; gap: 0.7rem; align-items: flex-start;
  padding: 0.5rem 0.2rem; border-bottom: 1px dashed var(--aiops-border);
}
.trace-step:last-child { border-bottom: none; }
.trace-agent {
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.76rem;
  font-weight: 660; color: var(--aiops-accent); min-width: 118px;
}
.trace-summary { font-size: 0.83rem; flex: 1; }
.trace-meta { font-size: 0.72rem; color: var(--aiops-muted); white-space: nowrap; }

/* ---- citation ---- */
.cite {
  border: 1px solid var(--aiops-border); border-radius: 9px;
  padding: 0.6rem 0.8rem; margin-bottom: 0.5rem; background: var(--aiops-surface);
}
.cite .ref {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 0.76rem; font-weight: 640; color: var(--aiops-accent);
}
.cite .snip { font-size: 0.79rem; color: var(--aiops-muted); margin-top: 0.25rem; line-height: 1.45; }

/* ---- misc ---- */
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.78rem; }
.note {
  border-left: 3px solid var(--aiops-warn); background: rgba(214,138,30,.07);
  padding: 0.6rem 0.85rem; border-radius: 0 8px 8px 0; font-size: 0.83rem;
}
.stTabs [data-baseweb="tab"] { font-size: 0.9rem; font-weight: 600; }
div[data-testid="stMetricValue"] { font-size: 1.4rem; }
</style>
"""


def card(label: str, value: str, foot: str = "") -> str:
    return (
        f'<div class="aiops-card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        + (f'<div class="foot">{foot}</div>' if foot else "")
        + "</div>"
    )


def cards(items: list[tuple[str, str, str]]) -> str:
    body = "".join(card(label, value, foot) for label, value, foot in items)
    return f'<div class="aiops-cards">{body}</div>'


def pill(verdict: str) -> str:
    cls = {"answered": "pill-answered", "escalated": "pill-escalated", "blocked": "pill-blocked"}.get(
        verdict, "pill-neutral"
    )
    return f'<span class="pill {cls}">{verdict.upper()}</span>'
