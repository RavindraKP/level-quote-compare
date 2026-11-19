# Notes for coding agents reviewing this repository

## Verify in two minutes, no API key needed

```bash
uv sync
uv run pytest -q                           # about 350 tests; live-model tests skip without a key
uv run ruff check .
uv run python -m evals.run --seed A --fake # scoring harness against a fake system
```

With `LEVEL_ANTHROPIC_API_KEY` set, `uv run python -m evals.run --seed A` scores the real pipeline (about $1–2). Never set or read `ANTHROPIC_API_KEY`; the app ignores it.

## Where to start reading

1. `README.md`: what the product does, the ten rules and where each is enforced.
2. `level/types.py`: the shared contracts.
3. `level/pipeline/run.py`: how one reply is read, end to end.
4. `level/core/cells.py` and `level/core/normalise.py`: how a quoted price becomes a comparable, status-carrying number.
5. `level/core/scenario.py`: the award calculator the analyst calls.
6. `level/agents/analyst/`: the analyst's tool loop and grounding check (`grounding.py`).
7. `evals/run.py`, `evals/extraction.py`: how scores are computed against the answer key.

## Claims worth checking

- **No model arithmetic reaches the screen:** search `level/agents/` and `level/pipeline/prompts/` for any instruction to compute; totals come from `level/core/`.
- **No hardcoding:** fixture vendor names, file names and demo questions should not appear in `level/` or in prompts. `data/` and `evals/` hold them; `scripts/` generates them.
- **Unknown stays unknown:** look for any path that turns a missing price or freight into zero (`level/core/cells.py`, `level/core/landed.py`, `level/core/scenario.py`).
- **Vendor isolation:** `level/agents/vendor_copilot/` and `tests/test_vendor_copilot.py`.
- **Unseen data:** `evals/run.py` refuses seeds other than A without `--allow-unseen`. Final results for unseen sets are in `evals/results/`.

## Structure rules

- `level/` never imports Streamlit. `app/` pages hold layout and calls only.
- Durable state is in SQLite (`level/db.py`); long work goes through `level/worker.py`.
- Every word a user sees comes from `level/copy.py` and follows `docs/UX_GUIDE.md`.
- Design choices and their reasons are logged line by line in `DECISIONS.md`.
