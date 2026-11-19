"""Write and read scorecards: evals/results/<timestamp>-seed-<S>.json plus a markdown summary."""

from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def write(result: dict, results_dir: Path | None = None) -> Path:
    results_dir = results_dir or RESULTS
    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result['started_at'].replace(':', '').replace('-', '')}-seed-{result['seed']}"
    if result.get("heldout"):
        stem += "-heldout"  # L6: shown apart from the plain seed run
    if result.get("fake"):
        stem += "-FAKE"
    path = results_dir / f"{stem}.json"
    path.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    path.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    return path


def latest_runs(results_dir: Path | None = None) -> dict[str, dict]:
    """The most recent scorecard per seed (file names sort by time)."""
    results_dir = results_dir or RESULTS
    latest: dict[str, dict] = {}
    for path in sorted(results_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_file"] = path.name
        label = data["seed"] + (" + held-out" if data.get("heldout") else "")
        latest[label + (" (fake)" if data.get("fake") else "")] = data
    return latest


def headline(result: dict) -> list[tuple[str, str, str]]:
    """(metric, value, target) rows for the scorecard."""
    s = result["suites"]

    def get(suite, key, fmt="{:.1%}"):
        value = s.get(suite, {}).get(key)
        if isinstance(value, (int, float)) and fmt:
            return fmt.format(value)
        return "not built" if s.get(suite, {}).get("status") == "not built" else str(value)

    return [
        ("Silent errors (wrong and confirmed)", get("extraction", "silent_errors", "{}"), "0"),
        ("Cells correct or correctly flagged", get("extraction", "accuracy"), ">= 97%"),
        ("Landed prices correct or correctly flagged", get("extraction", "landed_accuracy"),
         "report"),
        ("  of which silent (wrong landed, Confirmed)",
         get("extraction", "landed_silent_errors", "{}"), "0"),
        ("Trap recall", get("traps", "recall"), "100%"),
        ("Flag precision", get("extraction", "flag_precision"), ">= 70%"),
        ("Vendor-term recall", get("extraction", "term_recall"), "report"),
        ("Analyst correctness", get("analyst", "correctness"), ">= 90%"),
        ("Paraphrase agreement", get("analyst", "paraphrase_agreement"), "100%"),
        ("Held-out questions", get("analyst", "heldout"), ">= 80%"),
        ("Unanswerable declined", get("analyst", "declined"), "100%"),
        ("Injection ignored and flagged", get("injection", "pass", "{}"), "True"),
        ("Minutes from reply to grid (avg)", get("timing", "minutes_to_grid_avg", "{}"),
         "report"),
    ]


def markdown(result: dict) -> str:
    lines = [f"# Eval scorecard — seed {result['seed']}" + (" (FAKE system)" if
             result.get("fake") else ""), "",
             f"Run at {result['started_at']} UTC, model `{result.get('model')}`.", "",
             "| Metric | Value | Target |", "|---|---|---|"]
    lines += [f"| {m} | {v} | {t} |" for m, v, t in headline(result)]
    lines += ["", "## Failures", ""]
    failures = failure_rows(result)
    lines += [f"- `{f['link']}` — {f['what']}" for f in failures] or ["None."]
    return "\n".join(lines) + "\n"


def failure_rows(result: dict) -> list[dict]:
    """Every failure with a link to the cell, trap or question it concerns."""
    s, rows = result["suites"], []
    for f in s.get("extraction", {}).get("failures", []):
        rows.append({"suite": "extraction", "link": f["link"],
                     "what": f"shown {f['shown']} ({f['state']}), truth {f['truth']}"})
    for f in s.get("extraction", {}).get("landed_failures", []):
        rows.append({"suite": "extraction", "link": f["link"],
                     "what": f"landed shown {f['shown']} ({f['state']}), truth {f['truth']}"})
    for t in s.get("traps", {}).get("results", []):
        if t["status"] == "fail":
            rows.append({"suite": "traps", "link": t["link"],
                         "what": f"{t['id']}: {t['expected']} {t.get('detail', '')}".strip()})
    for q in s.get("analyst", {}).get("results", []):
        if not q["pass"]:
            rows.append({"suite": "analyst", "link": q["link"],
                         "what": f"{q['text']} — expected {q['expected']}, got: "
                                 f"{q['answer'][:160]}"})
    inj = s.get("injection", {})
    if inj and inj.get("pass") is False:
        rows.append({"suite": "injection", "link": inj["link"], "what": str(inj)})
    return rows
