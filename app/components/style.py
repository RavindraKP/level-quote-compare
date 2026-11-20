"""The one block of CSS for the whole app (docs/UX_GUIDE.md section 3). Layout only.

Colours and the font come from .streamlit/config.toml; this adds tighter spacing, summary
tiles, status chips, the event header and stepper, and hides Streamlit's own chrome.
"""

from html import escape

import streamlit as st

CSS = """
<style>
/* Streamlit's own menu, footer and deploy button are not part of the product. */
#MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stAppDeployButton"],
[data-testid="stToolbarActions"], [data-testid="stDecoration"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent; height: 0; }

/* Tighter page: the header replaces the big title. */
.block-container, [data-testid="stMainBlockContainer"] {
  padding-top: 1.2rem !important; padding-bottom: 2rem !important; max-width: 1480px; }
h1 { font-size: 1.55rem !important; font-weight: 600 !important; padding: 0.2rem 0 0.4rem; }
h2, h3 { font-weight: 600 !important; }
h2 { font-size: 1.2rem !important; } h3 { font-size: 1.05rem !important; }
body, .stMarkdown, [data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }

/* Event header */
.lv-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem 1rem;
  border-bottom: 1px solid #E3E7EB; padding-bottom: 0.55rem; margin-bottom: 0.4rem; }
.lv-head .lv-title { font-size: 1.15rem; font-weight: 600; color: #1F2933; }
.lv-head .lv-meta { color: #52606D; font-size: 0.86rem; }
.lv-head .lv-meta + .lv-meta { border-left: 1px solid #CBD2D9; padding-left: 1rem; }

/* Status chips */
.lv-chip { display: inline-block; padding: 0.05rem 0.55rem; border-radius: 999px;
  font-size: 0.78rem; font-weight: 600; border: 1px solid transparent; white-space: nowrap; }
.lv-chip.teal { background: #E6F4F1; color: #0B5959; border-color: #B7E0D8; }
.lv-chip.grey { background: #F1F3F5; color: #3E4C59; border-color: #E3E7EB; }
.lv-chip.amber { background: #FFF6E0; color: #8A5A00; border-color: #F5DFA6; }
.lv-chip.red { background: #FDECEC; color: #9B1C1C; border-color: #F5C2C2; }

/* Stage stepper */
.lv-steps { display: flex; flex-wrap: wrap; gap: 0.25rem; margin: 0.15rem 0 0.9rem; }
.lv-step { font-size: 0.8rem; padding: 0.2rem 0.7rem; border-radius: 4px; color: #7B8794;
  background: #F4F6F8; text-decoration: none !important; }
.lv-step.done { color: #0B5959; background: #E6F4F1; }
.lv-step.now { color: #FFFFFF; background: #0F6E6E; font-weight: 600; }
.lv-step:hover { outline: 1px solid #0F6E6E; }
.lv-arrow { color: #CBD2D9; font-size: 0.8rem; align-self: center; }

/* Summary tiles */
.lv-tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 0.6rem; margin: 0.3rem 0 0.8rem; }
.lv-tile { border: 1px solid #E3E7EB; border-radius: 6px; padding: 0.6rem 0.8rem;
  background: #FFFFFF; }
.lv-tile.muted { background: #F7F8FA; color: #7B8794; }
.lv-tile .lv-k { font-size: 0.78rem; color: #52606D; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.lv-tile .lv-v { font-size: 1.25rem; font-weight: 600; color: #1F2933; text-align: right; }
.lv-tile .lv-s { font-size: 0.78rem; color: #52606D; text-align: right; }

/* Muted box for caveats and notes */
.lv-note { background: #F7F8FA; border-left: 3px solid #CBD2D9; padding: 0.5rem 0.8rem;
  color: #3E4C59; font-size: 0.86rem; border-radius: 0 4px 4px 0; margin: 0.4rem 0; }
.lv-issue-title { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.2rem; }
.lv-dim { color: #7B8794; font-size: 0.82rem; }

/* Review queue: list rows read as a list, not as buttons */
.st-key-lv-queue button { justify-content: flex-start !important; text-align: left !important;
  min-height: 2rem; padding: 0.25rem 0.55rem !important; }
.st-key-lv-queue .stButton, .st-key-lv-queue button { width: 100% !important; }
.st-key-lv-queue button * { justify-content: flex-start !important; text-align: left !important; }
.st-key-lv-queue button > div, .st-key-lv-queue button > div > span { width: 100%; }
.st-key-lv-queue button[kind="tertiary"]:hover { background: #F1F5F7; }
.st-key-lv-queue button p { text-align: left !important; font-size: 0.86rem; }
.st-key-lv-queue [data-testid="stVerticalBlock"] { gap: 0.15rem; }
.lv-vhead { font-weight: 600; color: #1F2933; margin: 0.7rem 0 0.15rem; font-size: 0.92rem;
  border-bottom: 1px solid #E3E7EB; padding-bottom: 0.15rem; }
.lv-vhead .lv-dim { font-weight: 400; margin-left: 0.4rem; }
.lv-card-head { display: flex; flex-wrap: wrap; gap: 0.3rem 0.6rem; align-items: baseline; }
.lv-ctx { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
.lv-ctx td { padding: 0.25rem 0.4rem; border-bottom: 1px solid #EEF1F4; vertical-align: top; }
.lv-ctx td.k { color: #52606D; width: 32%; }
.lv-ctx td.n { text-align: right; white-space: nowrap; }
.lv-quote { background: #FFF8E1; border-left: 3px solid #F5C451; padding: 0.4rem 0.7rem;
  border-radius: 0 4px 4px 0; margin: 0.2rem 0 0.3rem; }

/* Detail panel beside a grid (components/side_panel.py). The column holding the panel
   stays in view while the page scrolls and scrolls on its own when taller than the window. */
[data-testid="stColumn"]:has([class*="st-key-lv-panel-"]) {
  position: sticky; top: 0.75rem; align-self: flex-start;
  max-height: calc(100vh - 1.5rem); overflow-y: auto; overflow-x: hidden; }
[class*="st-key-lv-panel-"] { background: #FFFFFF; min-width: 0; }
[class*="st-key-lv-close-"] { flex: 0 0 auto !important; width: auto !important; }
[class*="st-key-lv-close-"] button { white-space: nowrap; }
[class*="st-key-lv-panel-"] [data-testid="stImage"] img,
[class*="st-key-lv-panel-"] img { max-width: 100%; height: auto; }
[class*="st-key-lv-panel-"] pre, [class*="st-key-lv-panel-"] code { white-space: pre-wrap;
  word-break: break-word; }

/* Compare: sections and tabs read as separate parts of the page */
[data-testid="stTabs"] [role="tablist"] { border-bottom: 2px solid #E3E7EB; gap: 1.2rem; }
[data-testid="stTabs"] [role="tab"] p { font-size: 1rem; font-weight: 600; }

/* Sidebar: section labels as small caps */
[data-testid="stSidebarNav"] header, [data-testid="stNavSectionHeader"] {
  text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.72rem !important;
  color: #7B8794 !important; }
</style>
"""


def apply() -> None:
    """Inject the stylesheet. Called once per run from Home.py."""
    st.html(CSS)


def chip(text: str, tone: str = "grey") -> str:
    """HTML for one status chip; tone is teal, grey, amber or red."""
    return f'<span class="lv-chip {tone}">{escape(text)}</span>'


def tiles(items: list[tuple[str, str, str]], muted: set[int] | None = None) -> None:
    """A row of summary tiles: (label, value, small line under it). Text only, no maths."""
    html = "".join(
        f'<div class="lv-tile{" muted" if i in (muted or set()) else ""}">'
        f'<div class="lv-k" title="{escape(k)}">{escape(k)}</div>'
        f'<div class="lv-v">{escape(v)}</div><div class="lv-s">{escape(s)}</div></div>'
        for i, (k, v, s) in enumerate(items))
    st.html(f'<div class="lv-tiles">{html}</div>')


def note(text: str) -> None:
    """A muted box, used for caveats under an answer."""
    st.html(f'<div class="lv-note">{text}</div>')
