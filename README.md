# Level

Level reads vendor quotes in any format (Excel, PDF, Word, a phone photo, a three-line email), lines them up on one basis with a receipt behind every number, and lets a buyer ask questions of the comparison in plain language, through to a defensible award.

It was built as a product prototype for the brief "Kill the quote spreadsheet": a buyer sends an RFx, five vendors reply in five formats, and the buyer loses three days retyping them before anyone can compare.

- **Live app:** https://level-5tg8.onrender.com (passcode shared separately)
- **One-page note:** [docs/ONE_PAGER.md](docs/ONE_PAGER.md)
- **Final scores:** [evals/results/](evals/results/)
- **QA summary:** [docs/QA_SUMMARY.md](docs/QA_SUMMARY.md)
- **Manuals** (buyer, vendor, product, technical): [docs/manuals/](docs/manuals/README.md)
- **Decisions log:** [DECISIONS.md](DECISIONS.md)
- **For coding agents:** [AGENTS.md](AGENTS.md)

## What it does

1. **Draft:** an RFx co-pilot drafts lines, questionnaire and terms, or starts from last year's event. Only the buyer can send.
2. **Read:** each reply is read in the background, whatever its format. Photos are read twice and compared.
3. **Review:** only the points the checks could not settle reach a person, most money at stake first.
4. **Compare:** every vendor on the same lines, unit and currency. Every price has a status and a receipt: the vendor's own words, then each conversion step.
5. **Ask:** an analyst answers questions in plain language ("split it, cheapest per line, only vendors who cleared the questionnaire") through a tested award calculator, with cautions attached by code.
6. **Award:** compare saved options and the vendor split, confirm the award, and export a memo whose first page states how much of the award rests on prices nobody has verified yet.
7. **Vendor link:** each vendor gets an optional personal link that asks only the open questions about their own quote. A vendor never sees another vendor's data.

## The ten rules the code follows

| # | Rule | Where it is enforced |
|---|---|---|
| 1 | The AI loops are real: no hardcoded results, no fixture names, files, lines or demo questions in any prompt or code path | `tests/` (prompt and code scans), `evals/` on unseen data |
| 2 | The model never does arithmetic that reaches the screen | `level/core/` (pure, tested functions) |
| 3 | No number without a receipt: every value carries a verbatim quote and locator; failing the evidence check means it is not shown as verified | `level/pipeline/verify.py`, `level/core/cells.py` |
| 4 | Unknown stays unknown: a missing price is "not quoted", never zero or an average | `level/core/cells.py`, `level/core/scenario.py` |
| 5 | Status comes from checks, not from the model's confidence | `level/core/checks.py`, `level/core/cells.py` |
| 6 | Vendor documents are untrusted input: wrapped, labelled, instructions inside them ignored | `level/pipeline/prompts/`, `level/pipeline/intake.py` |
| 7 | Answer keys are used only by the generator and the evals; unseen sets are scored only with `--allow-unseen` | `scripts/`, `evals/run.py` |
| 8 | Chat cannot change extracted data: what-ifs are scenario parameters | `level/agents/analyst/`, `level/core/scenario.py` |
| 9 | The engine is category-neutral: nothing in `level/` assumes a product category | `level/core/normalise.py` (conversions come from the RFx) |
| 10 | The vendor link never leaks other vendors' data, rankings or price feedback | `level/agents/vendor_copilot/`, `tests/test_vendor_copilot.py` |

## How it was built

Level was built by one person working with AI coding agents (Claude Code), in separate sessions for each part: data model and dataset, reading pipeline, calculation core, co-pilots, UI, evals, QA and UX audit. Every session worked under the same ten written rules above, recorded its choices in [DECISIONS.md](DECISIONS.md), and handed over through pull requests that were reviewed before merging.

Quality was held by three things rather than by trust in the agents:

- **Tested code for every number:** all arithmetic is in `level/core/` with unit tests (about 340 in total).
- **Evals against an answer key the system never sees:** a generator writes the vendor documents and the ground truth from a seed, with 26 planted "ugly edges". The final runs cover the demo data, a second set of quotes never used while building, and a sixth vendor layout nobody had seen.
- **Independent QA passes** in a real browser, which found and fixed blockers. One change that lowered scores on unseen quotes was reverted rather than tuned to the answer key.

The app uses the lowest-cost model (`claude-haiku-5-5`) throughout; the whole build's API spend was about $10.

## Final scores

| | Demo data | Unseen quotes | New vendor layout |
|---|---|---|---|
| Wrong prices shown as verified | 0 | 0 | 0 |
| Prices correct, or marked "check" | 100% | 100% | 100% |
| Ugly edges handled (26 planted) | 100% | 100% | 100% |
| Questions worth the buyer's time (target 70%) | 63% | 67% | 60% |
| The VP's questions answered correctly | 90% | 95% | 95% |
| Says "we don't have that" instead of guessing | 100% | 100% | 100% |

Source files: `evals/results/20261119T115924-seed-A.md`, `20261119T112400-seed-B.md`, `20261119T114825-seed-A-heldout.md`. Below target: the system raises more review items than it needs to, and on photos and scans the count varies from run to run.

## Run it locally

You need Python 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/) and, for anything that calls the model, an Anthropic API key set as `LEVEL_ANTHROPIC_API_KEY` (Level never reads `ANTHROPIC_API_KEY`).

```bash
cp .env.example .env                       # then fill in LEVEL_ANTHROPIC_API_KEY
uv sync
uv run python -m level.db reset            # empty database with the schema
uv run python scripts/seed.py --seed A     # demo RFx, 30 lines, 5 vendors, their replies (unread)
uv run python scripts/read_inbox.py        # read every reply with the model (a few minutes)
uv run streamlit run app/Home.py           # then open http://localhost:8501
```

On Windows PowerShell, use `Copy-Item .env.example .env` for the first line. Set `LEVEL_PASSCODE` in `.env` to put a passcode on the app.

### Checks that need no API key

```bash
uv run pytest -q                           # unit tests (live-model tests skip without a key)
uv run ruff check .
uv run python -m evals.run --seed A --fake # checks the scoring harness with a fake system
```

### Scoring with the model

```bash
uv run python -m evals.run --seed A        # demo data, about $1–2
uv run python scripts/generate_dataset.py --seed 7
uv run python -m evals.run --seed 7 --allow-unseen   # any new seed: different prices, gaps and traps
```

Each run writes `evals/results/<time>-seed-<S>.md` and `.json`; the latest per seed shows on the app's Evals page.

### Docker and hosting

```bash
docker build -t level .
docker run -p 8501:8501 --env-file .env -v level-data:/var/level level
```

Run exactly one container: each process owns its background worker and the SQLite file. Hosting on Render: [docs/DEPLOY.md](docs/DEPLOY.md) and `render.yaml`.

## Where the AI loops are

| Loop | Code | What the model does | What code does |
|---|---|---|---|
| Reading a reply | `level/pipeline/` | Copies prices, units, terms and answers with the vendor's exact words; maps rows to RFx lines; reads photos twice | Evidence check, unit and currency conversion, arithmetic, plausibility and spec checks, price statuses |
| RFx co-pilot | `level/agents/copilot/` | Edits the draft through tools | Completeness check; sending is a button only |
| Analyst | `level/agents/analyst/` | Picks tools: read-only SQL, award options, tables, charts | Award calculation, totals, caveats; every figure in an answer must trace to a tool result |
| Vendor reply link | `level/agents/vendor_copilot/` | Asks the open questions, records answers | Questions come from coded checks; the vendor sees only their own quote |

## Layout

```
level/            no Streamlit imports; testable on its own
  types.py        shared Pydantic contracts
  db.py           SQLite schema, connections (normal + read-only), reset
  llm.py          the only Anthropic SDK wrapper
  worker.py       background job queue
  copy.py         every word a buyer or vendor reads
  pipeline/       intake → extract → match → verify, photo double-read
  core/           pure calculations: normalise, landed price, checks, cells, award scenarios
  agents/         RFx co-pilot, analyst, vendor co-pilot
app/              Streamlit pages: layout and calls only
scripts/          dataset generator, seed loader, inbox reader
evals/            scorecard and final results
data/             demo fixtures (seed A, held-out vendor), ground truth, last year's history
tests/
docs/             one-page note, manuals, UX guide, dataset, deploy, QA summary
```

All company and vendor names in the data are fictional.
