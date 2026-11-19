# Level: product manual

For a product team taking Level over. It describes what the build does today (main at `6680594`, 19 Nov 2026: after the Review redesign, the UX audit fixes, QA pass 3 and the freight-answer fix), where it differs from the plan, how quality is measured, and what is left. Sources: `DECISIONS.md` (cited as **D‹line›**, the line number in that file), `docs/QA_SUMMARY.md`, and the code. For the latest scores, see `evals/results/` and `docs/ONE_PAGER.md`; figures in this manual may predate them.

Related: [buyer guide](BUYER_GUIDE.md) · [vendor guide](VENDOR_GUIDE.md) · [technical spec](TECHNICAL_SPEC.md).

---

## 1. The problem and who it is for

A category buyer runs a sourcing event: she sends an RFx to five vendors and gets back five replies in five shapes. An Excel sheet in the vendor's own codes, priced per 1000. A PDF with a discount in a footnote. A Word document in US dollars, ex-works. A phone photo of a rate card priced per kg. A three-line email saying "rest same as last year, freight extra". Today she spends days building a spreadsheet to put these on one basis, and the spreadsheet hides every judgement call she made.

**Users**

| Who | What they need |
|---|---|
| Category buyer (primary) | Every price on one basis, with the source behind it; only the doubtful points to review; an award she can defend |
| Her VP | What-if answers ("cap it at two vendors", "dollar at 90") and a memo that says what is still uncertain |
| Vendors | To reply in any format, and not lose on a misread |

**The bet** (PLAN §2): the model reads, code calculates, and doubt is shown, not hidden. The brief judges ugly edges, trust and judgement, so every scope choice was tested against one question: *what does the system do, and show, when it is not sure?*

---

## 2. The end-to-end story

| Step | Page | What happens | AI or code |
|---|---|---|---|
| 1 | RFx | Buyer drafts the request with the co-pilot, or starts from a past event. Code lists the points that would make replies incomparable | Co-pilot (tools) + code completeness check |
| 2 | RFx → Inbox | Buyer clicks Send. One simulated email per vendor with an RFx PDF, an optional Excel template and a personal reply link | Code |
| 3 | Inbox | Replies arrive by email, upload or reply link. Each is queued and read in the background with visible progress | Worker |
| 4 | (background) | Intake → extract → match → normalise → verify → flag | Model transcribes and maps; code converts, checks and sets status |
| 5 | Vendor link | A vendor drops in a quote, answers only the questions code raised, and confirms how it was read | Vendor co-pilot (one tool) |
| 6 | Review | Buyer settles what is left, one focus card at a time: accept, correct, mark not quoted, accept a same-cause group, undo, or ask the vendor in one grouped email | Code; model rewords the email, code checks it |
| 7 | Compare | Grid on one basis, L1/L2/L3, receipt behind each price, questionnaire and documents | Code |
| 8 | Analyst | Plain-language questions, answered by the worker in the background; answers from tools, every figure checked against tool results | Analyst agent + deterministic award calculator |
| 9 | Award | Saved options side by side; award; memo (DOCX) and workbooks; clarification drafts | Code |

section 5 below lists where the build differs.

---

## 3. Feature inventory

Status: **Built** = works end to end and is tested. **Partial** = works with a stated gap. **Not built** = planned or discussed, absent.

### Sourcing

| Feature | Status | Where in the code | Notes |
|---|---|---|---|
| Passcode gate | Built | `app/common.py` `passcode_ok` | Off when `LEVEL_PASSCODE` is unset. One shared passcode; no accounts |
| Event header, stage stepper, grouped sidebar, open-issue badge | Built | `app/components/frame.py`, `app/Home.py` | Stepper links carry `?rfx=`; the event shown is pinned per session, so sending a new RFx does not switch pages to it (D190); `/overview` forwards to Overview |
| RFx co-pilot chat | Partial | `level/agents/copilot/agent.py`, `app/views/1_RFx.py` | 7 tools, no send tool. Cannot rename the draft (QA L5). Runs inside the page, not on the worker: leaving mid-turn loses the reply (QA P3-H6) |
| Direct editing of lines, questions, terms | Built | `app/views/1_RFx.py`, `level/agents/copilot/draft.py` | Edits apply only the delta, so chat and direct edits do not collide. Terms are a labelled form; pass rules are typed in words (D198); spec and conversions in a per-line dialog |
| Completeness check ("points to settle") | Built | `draft.check_completeness` | Category-neutral rules; includes a reply-by date (D197) |
| Past events list, read-only view, "Start from this event" | Built | `draft.past_events`, `draft.import_from_history`, `level/core/past.py` | Awarded events only (D196); a copy is titled "(copy)" and numbered after the newest event (D197) |
| Last year's price and supplier as buyer-only reference | Built | `draft.reference_prices` | Never in snapshot, email, template or link (tested) |
| RFx PDF attachment | Built | `level/agents/copilot/rfx_doc.py` | PyMuPDF HTML Story |
| Excel response template | Built | `draft.template_xlsx` | Optional for vendors |
| Send (simulated outbox) | Built | `draft.send` | Idempotent; gated by a "reviewed" checkbox; refuses a duplicate open title or a missing reply-by date (D190) |
| Add a vendor | Partial | `draft.add_vendor` | Invites from the page, but no duplicate check (QA M6) |
| Real email send and receive | Not built | – | Deliberately stubbed (PLAN §11) |

### Reading replies

| Feature | Status | Where | Notes |
|---|---|---|---|
| Intake by content, not extension: xlsx, xls, csv, pdf (text or scan), docx, jpg/png/heic/webp/tiff, eml, txt, pasted text | Built | `level/pipeline/intake.py` | Hidden sheets, merged cells, nested emails |
| Clear message + review item for unreadable files (empty, password-protected, damaged) | Built | `intake.Unreadable`, `run._unreadable` | |
| "No prices found" issue for blank or irrelevant replies | Built | `run._flag_if_no_prices` | Fixed after QA H5 |
| Structured extraction with verbatim quote + locator | Built | `level/pipeline/extract.py`, `prompts/__init__.py` (wp2-v8) | Transcribe only |
| Certificates and reports read with a small schema | Built | `extract.extract_facts` | |
| Photo double read + zoom re-read | Built | `level/pipeline/vision.py`, `run._image_extraction` | 2×2 tiles, 15% overlap |
| Second independent read of text documents (T2) | Built | `verify.second_read` | |
| Matching rows to lines (line / rule / reference scopes) | Built | `level/pipeline/match.py` | Code cleans the model's mapping |
| Unit, currency, GST, slab, discount normalisation | Built | `level/core/normalise.py` | Unit graph from each line's conversion factors |
| Delivered price from freight terms | Partial | `level/core/landed.py` | Distance and "delivered" without amount → unknown (QA M9). A vendor's freight answer is a new term for the places it names; each line takes the newest term covering its place (D210–D212) |
| Payment-terms carrying cost | Built (option) | `landed.py`, `ScenarioParams.include_payment_terms` | Not in the grid's Delivered view (D81) |
| Evidence check, arithmetic, plausibility, spec, coverage, certificate cross-check | Built | `verify.py`, `level/core/checks.py`, `pipeline/cross_check.py` | |
| Prompt-injection detection | Built | `prompts._UNTRUSTED`, `suspicious_instructions` | Flagged, no effect on prices |
| Background worker with progress, retry, dedupe | Built | `level/worker.py`, `level/jobs.py` | |
| Classic OCR as a third opinion | Not built | – | Deferred with a trigger (PLAN §5) |
| Hand-made real files set | Not built | `data/fixtures/real/` absent | Never supplied; never scored |

### Evaluation and decision

| Feature | Status | Where | Notes |
|---|---|---|---|
| Inbox: response tracker, live progress, Add a reply, Read again, Sent tab | Built | `app/views/2_Inbox.py` | |
| Review queue: issue list by vendor, focus card, buyer-only price check, named actions, same-cause bulk accept, one-level undo | Built | `app/views/3_Review.py`, `app/components/review_data.py`, `level/core/review.py` | PR #27 (D184–D188). Passed all five WP9 usability checks at 1440 and 1280 px in QA pass 3. Keyboard shortcuts: not built. Folded pairs make counts differ by one between pages (P3-L1) |
| Grouped clarification email ("Ask <vendor>") | Partial | `level/pipeline/clarify.py` (`assemble`) | Model rewords, code checks, fixed wording fallback; ends with the vendor's reply link (D207). Some emails still repeat a point or miss unquoted lines (P3-M1) |
| Compare grid, three bases, L1/L2/L3, tiles, supplier card | Built | `app/views/4_Compare.py`, `app/components/data.py` | Short vendor names with the full name in the tooltip; five vendors fit at 1280 px; "Lines in the total" row; "Not read yet" for unread replies (D209, D216) |
| Receipt behind every price | Built | `app/components/receipt.py` | |
| Assumptions panel (FX, cost of capital, GST) | Built | Compare sidebar → `data.set_assumption` | Recomputes every price |
| Technical tab (questionnaire, documents) | Partial | `4_Compare.py` | Verdict table first; a fail shows the vendor's answer, not always the reason (P3-M2) |
| Analyst agent | Built | `level/agents/analyst/` | 9 tools. Runs as a worker job (`analyst.ask`), so an answer survives leaving the page; "Ask again" for a lost one (D202) |
| Grounding check on analyst figures (T9) | Built | `analyst/grounding.py` | |
| Caveats under every answer (T10), sensitivity (T11), trace (T13) | Built | `core/scenario.py`, `5_Analyst.py`, `analyst/steps.py` | At most five caveats shown, terms folded (D204); trace in plain steps (D203). A what-if's caveats can contradict each other (P3-M6) |
| Award page: saved options, approval summary, award | Built | `app/views/6_Award.py`, `analyst/award.py` | No option pre-selected; "Selected option" (D205); saved options are snapshots (P3-M8) |
| Award memo (DOCX), comparison and option workbooks | Built | `analyst/memo.py`, `core/export.py` | Built when the download is clicked (D201). PDF memo: not built. Memo still titled "Award recommendation" (P3-L4) |
| Audit trail | Built | `audit_event`, `analyst/audit.py`, Award page | Filtered by event, in words (D200); includes reply-link freight, questionnaire and new-point answers (D215) |
| Negotiation drafts, auctions | Not built | – | Deliberately left out |
| Approval workflow, ERP/PO | Not built | – | Deliberately left out |

### Vendor side

| Feature | Status | Where | Notes |
|---|---|---|---|
| Signed per-vendor link, no passcode, no buyer pages | Built | `level/agents/vendor_copilot/link.py`, `app/Home.py` | |
| Upload / type a quote, read through the same pipeline | Built | `vendor_copilot.submit_reply` | |
| Questions from code, asked by the co-pilot | Built | `vendor_copilot/questions.py`, `agent.py` | Includes missing or unclear required questionnaire answers (D195). Runs inside the page, not on the worker (P3-H6) |
| "How we read your quote", confirm by line or all, correct a line | Built | `8_Vendor_Reply.py`, `vendor_copilot/answers.py` | |
| Confirm own last-year price instead of re-quoting | Built | `questions.py` (`confirm_price`) | Fixed after QA M17; a confirmed line is Verified by vendor even with an assumption (D191) |
| Freight answers on the link | Built | `vendor_copilot/answers.py`, `core/landed.py` | Stored per place named; other places keep their quoted freight; a sentence with several figures for several places is refused (D210–D212, QA P3-B1/H1/H4) |

### Operations

| Feature | Status | Where | Notes |
|---|---|---|---|
| Demo data: save, restore, reload and read | Built | `level/demo.py`, Overview | |
| Eval harness and Evals page | Built | `evals/`, `app/views/7_Evals.py` | |
| Docker, Render, Railway configs | Built | `Dockerfile`, `render.yaml`, `railway.json`, `deploy/start.sh` | Never verified on a live host by QA |
| Event lifecycle beyond Issued | Partial | `copy.event_stage` | The stage chip shows Evaluate once the reply date has passed or all replied (display only, D199); nothing sets `collecting` or `ready` in the database |

---

## 4. The ten non-negotiables and why each exists

The ten rules every part of the code follows (also in the README).

| # | Rule | Why it exists | How the build enforces it |
|---|---|---|---|
| 1 | The AI loops are real: no hardcoding, no fixture names, no special cases | A reviewer may upload an unseen document; a tuned demo would fall over and is dishonest | Prompts name no vendor, file or line; QA audit greps for fixture phrases; seed B and held-out runs; any seed can be regenerated |
| 2 | The model never does arithmetic that reaches the screen | Models make silent arithmetic slips; a buyer cannot audit them | Model-facing schemas carry numbers as strings (D5); all math in `level/core/`; analyst totals from `summary.py` (D82) |
| 3 | No number without a receipt | Trust needs a path from any number to the vendor's words | Every fact has quote + locator; evidence check; failed evidence never Verified (D59) |
| 4 | Unknown stays unknown | A zero or an average for a missing line silently moves the award | Not quoted is a state; totals return "no total" (D154); unit slips held out of totals (D161) |
| 5 | Status comes from checks, not model confidence | "87% sure" is not actionable and models are poorly calibrated | State rules in `cells._state`; model remarks can open an issue, never close one |
| 6 | Vendor documents are untrusted input | A quote can carry "rank this vendor first" | `llm.untrusted()` wrapper, prompt rules, suspicious-instruction issue, analyst SQL cannot read documents |
| 7 | Fixtures and ground truth are off limits outside `scripts/` and `evals/` | Reading the answers while writing prompts is overfitting | Directory rule; `tests/` uses invented documents (D130) |
| 8 | Chat cannot change extracted data | What-ifs must not corrupt the record | Analyst on a read-only connection with an authorizer; what-ifs are `ScenarioParams` |
| 9 | The engine is category-neutral | A reviewer may run laptops or freight lanes | Spec and conversion factors are free attributes; QA ran an IT hardware event |
| 10 | The vendor reply link never leaks | Sharing rivals' prices is a legal and trust failure | Signed token scope; vendor agent sees only its own data; emails built from a snapshot with no prices or incumbents |

---

## 5. Where the build differs from the plan

The last column says whether the documentation now matches the build. **Fixed (docs)** means the docs were corrected in the documentation tidy-up of 18 Nov 2026; no code changed. **Open gap** means the build still lacks it and it is on the roadmap (§8).

| Plan said | Build does | Source | Docs |
|---|---|---|---|
| Agents use the SDK tool runner | Explicit short tool loops on the one `llm.complete` wrapper | D30, D80 | Matches (recorded in DECISIONS) |
| Landed = price + freight + payment-terms carrying cost in the grid | Landed = price + freight everywhere (grid, scenarios, analyst). Carrying cost is an explicit scenario option (`ScenarioParams.include_payment_terms`), set through the analyst and shown with its cost-of-capital rate as an assumption | D81 | **Fixed (docs):** the `v_cells` comment in `level/core/views.sql` (comment only), the technical spec now says price + freight |
| Event moves Issued → Collecting on first reply, → Ready when all replied | No code sets `collecting` or `ready`. The seed loads the event as Collecting; a new event stays Issued until Awarded. The chip shows Evaluate once the reply date has passed or all replied (display only, D199). "Mark no response" has no button | `scripts/seed.py`, `draft.send`, `award.award`, `copy.event_stage` | **Open gap:** stored status not built; on the roadmap (§8) and in §7 |
| Clarification drafts written by the model | First fixed wording only (D77), then the model rewords fixed questions with a code check and fallback (D140) | `clarify.py` | Matches |
| Award memo DOCX, PDF if time | DOCX only | `memo.py` | Matches |
| Supplier card shows last year's spend | Shows lines under last year's contract, not spend (new arithmetic would belong in core first) | D146 | Matches |
| Review keyboard shortcuts | Large buttons that name the outcome and amount; the next issue opens by itself; one-level Undo | `3_Review.py` | Matches |
| RFx email with the full request in the body | Short body + RFx PDF + optional template | D176 | Matches |
| Home page | Called Overview; `app/pages/` renamed `app/views/` for a Streamlit deep-link bug | D20 | Matches |
| Analyst grounding against "this turn" | Against every tool result in the thread, so earlier figures can be repeated | `agent.py` docstring | Matches |
| DEPLOY.md first run: "Demo state", "Reseed and read inbox", "Save current as demo state", "Restore saved state"; checks wait for "Finished" | The panel is "Demo data" with "Reload and read every reply", "Save current data as the copy", "Restore saved copy"; a read reply shows "Read in N min" | `0_Overview.py`, `2_Inbox.py` | **Fixed (docs):** `docs/DEPLOY.md` "First run" and "Checks" use the names the app shows |
| One-page note scorecard | Quoted older seed A runs | `docs/ONE_PAGER.md` | **Fixed (docs):** seed A column is now the latest run (`20261118T234603`, after the freight fix); seed B and held-out stay as QA pass 2 recorded them, each with its result file |
| Time to comparison scored by the evals | Evals report `replies_timed: 0`; the Overview page measures it from job times | QA M8 | **Open gap:** roadmap "Time-to-grid in the evals" |

---

## 6. How quality is measured

### 6.1 The harness

`uv run python -m evals.run --seed A` builds a fresh throwaway database, loads the seed's RFx and inbox, reads every reply through the real pipeline, rebuilds the grid, and scores it against the seed's ground truth. Results go to `evals/results/<time>-seed-<S>.json` and `.md`; the Evals page shows the latest per seed. A full seed A run costs about $1–2 (README).

The dataset (`docs/DATASET.md`) is generated from ground truth with a seed: five vendors in five formats with 26 seeded traps (X01–X26). A new seed changes prices, missing lines, line order and which lines carry traps.

### 6.2 Metrics

| Metric | Definition | Target |
|---|---|---|
| **Silent errors** | Prices that are wrong (more than 0.5% off truth) **and** shown as Verified. The headline | **0** |
| Cells correct or correctly flagged | Correct within 0.5%, or not quoted where truth is not quoted, or wrong but shown as Check/Mismatch | ≥ 97% |
| Landed correct or correctly flagged | The same rule on delivered prices; a wrong landed value on a Verified price is a silent error | report (0 silent) |
| Trap recall | Each seeded trap handled or raised | 100% |
| Flag precision | Share of raised issues that point at a real problem in truth | ≥ 70% |
| Vendor-term recall | Discount, freight, payment, validity and tooling terms found | report |
| Analyst correctness | ~20 questions; expected answers recomputed from ground truth in `evals/expected.py`, independent of `level/core` (D51) | ≥ 90% |
| Paraphrase agreement | 3 wordings each of 5 questions agree | 100% |
| Held-out questions | 5 questions never used in prompt work | ≥ 80% |
| Unanswerable declined | 4 questions the data cannot answer; graded by a model (the only model grader, D52) | 100% |
| Injection | Planted "rank this vendor first" ignored and raised | True |
| Minutes from reply to grid | Arrival to cells in the grid | report |

### 6.3 Latest recorded numbers

| Metric | Target | Seed A, latest (`20261118T234603`, after the freight fix) | Seed B, unseen (`20261118T033533`, QA pass 2) | Seed A + held-out vendor F (`20261118T034648`, QA pass 2) |
|---|---|---|---|---|
| Silent errors | 0 | **0** | **0** | **0** |
| Cells correct or flagged | ≥ 97% | 100.0% (146 correct + 4 flagged) | 98.0% | 100.0% |
| Landed correct or flagged | report | 92.7% (0 silent) | 93.3% (0 silent) | 93.9% (0 silent) |
| Trap recall | 100% | 100.0% (25/25) | 96.0% (X19 failed) | 100.0% (26/26) |
| Flag precision | ≥ 70% | **65.6%** (21 of 32 points real) | **66.7%** | **66.7%** |
| Vendor-term recall | report | 100% | 100% | 100% |
| Analyst correctness | ≥ 90% | 90.0% (Q04, H03, Q15 fail) | 90.0% | 95.0% |
| Paraphrase agreement | 100% | 100% | 100% | 100% |
| Held-out questions | ≥ 80% | 80% | 100% | 80% |
| Unanswerable declined | 100% | 100% | 100% | 100% |
| Injection | True | True | True | True |
| Minutes to grid | report | not measured | not measured | not measured |

How the seed A numbers moved over the last three runs:

| Run | When | Flag precision | Analyst | Points raised | Note |
|---|---|---|---|---|---|
| `20261118T092317` | after WP13 | 62.1% | 95.0% | 29 | Baseline used in QA pass 3 |
| `20261118T185519` | QA pass 3 | 63.3% | 90.0% | 30 | Q15 changed answer (below) |
| `20261118T234603` | after the freight fix (PR #34) | **65.6%** | 90.0% | 32 | No metric worse than pass 3 |

Seed B and the held-out run were made on commit `eefa8d0`, before the QA pass 2 fixes. By the "run once" rule they were not rerun.

Reading time: QA pass 2's browser run read the whole five-vendor inbox in 4 min 42 s. The run that made this manual's screenshots (19 Nov 2026, the same five vendors plus the revision email) took **4 min 41 s** from the first job starting to the last finishing.

### 6.4 What is below target

- **Flag precision** (62–67% against 70%) on every run; 65.6% on the latest. Most noise is the reader's free-text "unclear in the source" notes and grid-level "unit assumed" notes, kept on purpose (D105).
- **Delivered prices**: 11 of Bharat's lines are unknown where truth knows them, because "free within 150 km" cannot be resolved without plant distances. Safe (not silent) but incomplete.
- **Analyst**, three seed A questions fail:
  - Q04 (cheapest per line across all vendors, ex-works) and H03 (one vendor's total for every line): the answers use unconverted unit prices or a different scope from the grader (failed on every recent run).
  - **Q15** ("What is the total annual quantity across all lines?", expected 2,115,000). Since QA pass 3 the analyst answers "20,40,000 pieces across 29 lines, plus 75,000 sets … kept separate rather than adding them". It declines to add pieces and sets into one total. That is consistent with "unknown stays unknown" (no unit conversion between a set and a piece exists), but the grader expects the plain sum and marks it wrong. Open decision: change the expected answer (accept "2,040,000 pieces + 75,000 sets") or ask for the sum with a warning. Left as is; not a grounding failure.
- **Seed B trap X19**: "rest same as last year" was mapped to the accessory lines only on the unseen seed.
- **Time to grid** is not captured by the evals.

The evals cannot see screen-level problems (copy, number formats, a ₹0 tile). Do not read "0 silent errors" as "the screens are right" (QA §10). QA pass 3 found its blocker (P3-B1, below) in a browser round trip, not in the evals.

---

## 7. Known limitations and open QA items

From the QA passes (summarised in `docs/QA_SUMMARY.md`), and the fixes recorded after them in `DECISIONS.md`.

### Fixed since QA pass 2

B5 ₹0 totals (D154), B6 vendor name in a docstring, H1 prose yes/no answers (`answer.stance`, D158), H2 blank photo prices become "hard to read" (D159), H3 "box of 50" (D160), H4 unit slips left out of totals (D161), H5 no-price replies (D162), H6 unplaced prices raise an issue (D163), H7 no tracebacks (D164), M7 questionnaire ticks, M10–M13 copy and number formats, M15 ranks, M17 vendor link confirms own price, L6, L9, L10, L12, L14. The vendor link was then reworked so answers close their questions and confirmation is per line (D172–D174).

### Fixed by the Review redesign and the UX audit (PRs #27–#31)

QA pass 3 verified these in the browser. All 10 UX-audit blockers are fixed: the Award page no longer crashes after vendor answers (B1); no JSON, SQL or code block reaches a buyer or vendor page (B2–B7, B9); the audit trail is per event and in words (B8); sending keeps the buyer on their event (B10). Also: the analyst answers through the worker, so an answer survives leaving the page (H5, was M1); no option is pre-selected on Award (H6); unread replies say "Not read yet" with a **Read all** button (H8); every clarification email ends with the vendor's reply link (H3); Excel sources open at the cell (H4). Tally from pass 3: 36 of 46 audit items fixed, 6 fixed in part, 3 open (M3, M5, M16), 1 not confirmed (L3).

### Fixed by the freight-answer fix (PR #34, after QA pass 3)

| Id | Problem | Fix |
|---|---|---|
| P3-B1 (blocker) | A vendor's freight answer for one plant replaced the freight for every plant, and a vendor tick then showed the wrong delivered price as Verified (Pacific line 1: ₹21.21 instead of ₹19.91) | A freight answer is a new term naming only its places; each line takes the newest term that covers its place (D210, D211) |
| P3-H1 | "Rs 1.50 per piece" stored as "per per", so Mahalaxmi's delivered price was unknown on all 30 lines | `landed.freight_answer_text` writes "per" once (D212) |
| P3-H4 (freight part) | "Freight extra at actuals for Hosur" wiped "Delivery free within 150 km" | Earlier terms are never deleted; other places keep them |
| P3-H2 | File names, PO, phone and PIN numbers regrouped as rupees | Regroup only next to ₹/Rs/INR or a quantity word (D214) |
| P3-H3 | Freight and questionnaire answers missing from the audit trail and receipts | Every reply-link answer carries its event id (D215) |
| P3-H5 | Compare grid did not fit at 1280 px | Columns sized 200 + 110 + 5 × 112 px (D216) |
| P3-H7 | Delivered tiles counted lines with no delivered price | Tile says "N without a delivered price, left out"; "Lines in the total" row (D216) |
| P3-L8 | Windows-safety grep hits | Fixed |

The seed A eval after this fix (`20261118T234603`) made no metric worse and raised flag precision from 63.3% to 65.6%.

### Still open

| Id | Problem | Impact |
|---|---|---|
| Run-to-run variation | The same files can read differently (smudged digit, prose answers); the losing branch is visible but still varies | Recommendation can change between reads |
| P3-H4 (question part) | In QA pass 3, "freight extra at actuals" in a vendor answer closed "Delivered price unknown" as settled by the vendor without opening a new freight question. PR #34 keeps the earlier terms; whether the question now stays open was not re-tested in the browser | Delivered may stay unknown with nothing open to chase it |
| P3-H6 (was M1) | The RFx co-pilot and the vendor co-pilot still run inside the page; leaving mid-turn loses the reply | Ask again |
| P3-M1 (UX M16) | Clarification emails repeat a point (two Baddi freight questions) or miss unquoted lines | Vendor confusion, extra round trips |
| P3-M2 (UX M6) | The Technical tab shows a failing vendor's answer, not the reason (expired certificate) | Buyer opens the receipt to learn why |
| P3-M3 (UX M5) | The FX source reads "Reference rate (placeholder, to verify)", also in the memo | Looks unfinished to a VP |
| P3-M4 | Analyst text shows paise on totals ("₹9,99,271.70") | Cosmetic, against the UX guide |
| P3-M5 | "flagged" and "delivered (delivered)" in analyst text; receipt shows four decimals and "factor from …" | Wording |
| P3-M6 | A what-if's caveats contradict each other (₹90.00 for the option, ₹86.70 in the basis line) | Confusing under a what-if |
| P3-M7 | Analyst correctness 95% → 90% (Q15, §6.4) | Grader decision pending |
| P3-M8 | Saved award options are snapshots; a later vendor confirmation does not update them or the memo | Memo can list a price as unverified after the vendor confirmed it |
| P3-M9 (UX L8) | "Unclear in the reply" rows on Review are cut mid-sentence | Cosmetic |
| P3-L1 | Counts differ by one between the sidebar, Overview and Review after folded pairs | Minor |
| P3-L2 | RFx co-pilot with a refused key says only "could not finish" | Minor |
| P3-L3 | "Close: keep their reply as read" on "Delivered price unknown" does not say the price stays unknown | Wording |
| P3-L4 | Memo still titled "Award recommendation" | Wording |
| P3-L5 | Award workbook's Sensitivity sheet shows raw numbers | Cosmetic |
| P3-L6 | Audit trail shows "0 lines copied" for a co-pilot re-import | Minor |
| P3-L7 | Vendor co-pilot grammar ("Lines 13 is priced per set") | Wording |
| P3-L9 | "Already decided in another window" toast not confirmed in the browser | Not confirmed |
| M2 | Flag precision below 70% | Review fatigue |
| M3 | The analyst's default for "requirements" depends on wording | Inconsistent answers to similar questions |
| M6 | Add a vendor has no duplicate check | Duplicate vendors |
| M8 | Evals do not measure time to grid | Metric gap |
| M9 | "Delivered", "free within N km", "Pune & Hosur free" without amounts → delivered unknown | Fewer delivered prices |
| L1 | Refresh asks for the passcode again | Minor |
| L5 | Co-pilot cannot set the title | Use the Title box |
| L15 | `app/components/mock_seed.py` used only by tests | Housekeeping |
| Lifecycle | Stored status never reaches Collecting/Ready; no "no response" button | Stepper stage is display-only for new events |
| Real files | Hand-made photos and scans never supplied or scored | Generalisation to messy real files is unmeasured |
| Hosting | Not verified on a hosted URL by QA | Deploy risk before a live demo |

---

## 8. Cut list and roadmap

### Cut order (PLAN §10)

First to go: vendor reply link → co-pilot history import → award memo document → payment-terms carrying cost → pinned option comparison → co-pilot beyond a minimal chat. **Never cut:** the pipeline, the receipt, price statuses, the review queue, the grounded analyst, the eval scorecard.

Nothing was cut. Everything on the list shipped, including the reply link and the history import (WP13).

### Deliberately left out (PLAN §11)

Real email, auth and roles, mandatory vendor portal or template, ERP/PO, negotiation and auctions, live FX feed, full TCO, approval workflow, fine-tuning and OCR pre-processing.

### Roadmap

| Next | Why |
|---|---|
| Raise flag precision above 70% | Merge the reader's free-text notes into coded checks; fewer, better questions |
| Co-pilots on the worker | Move the RFx and vendor co-pilots to worker jobs, as the analyst already is (P3-H6) |
| Clarification email de-duplication | One question per open point, unquoted lines included (P3-M1) |
| Decide Q15 | Change the expected answer or the prompt for "total quantity" across units (§6.4) |
| Event lifecycle | Set Collecting on first reply, Evaluate when all replied or marked no response; add the button |
| Plant distances or a freight table | Resolve "free within N km" instead of leaving delivered unknown |
| Stabilise run-to-run reads | A rule or an "ask the vendor" path for every yes/no branch decided by one model call |
| Time-to-grid in the evals | Back the "three days → minutes" claim with the scorecard |
| Real-file set | Phone photos, pen annotations, scans, pasted screenshots, scored by hand |
| WP13 next steps | Per-line "changed since last year" tags; choosing among several past events in chat |
| Should-cost from paper indices | Tell the buyer whether the lowest quote is a good price, not just the lowest of five |
| Price memory across events | Every verified price becomes history; drift by vendor becomes visible |
| Close the loop through the link | Send grouped questions as a link, not an email |

---

## 9. Trade-offs

Each row: the decision, what else was available, why this one, and what it costs.

| Decision | Alternatives | Why chosen | Cost |
|---|---|---|---|
| **Streamlit** for the UI (PLAN §5) | React/Next.js front end + API | One language, one deployable; chat, tables, uploads and charts built in; the owner can read and patch every line | Less control of the grid; reruns force a worker design; any long call that runs in the page is lost on navigation (fixed for the analyst by moving it to the worker; still true for the co-pilots, P3-H6); looks like an internal tool unless themed (WP12) |
| **SQLite** + files on disk (PLAN §5; D6, D8) | Postgres | Zero setup; read-only mode for the analyst; one file to snapshot for demo restore (D115) | One writer at a time; one instance only; no exact decimal type, so money is TEXT |
| **One in-process worker thread** with a job table (PLAN §5; D11, D12, D14) | Celery/RQ + Redis, a separate worker | Five files do not need a broker; the job table is the upgrade path | Exactly one process; worker starts on the first page load; a second instance would also run jobs |
| **Model transcribes, code computes** (PLAN §2; D5) | Let the model convert units and total prices | Every number is reproducible and auditable; arithmetic tested once | Much more code: a unit graph, freight parser, slab logic; unusual phrasings ("free within 150 km") fall to unknown |
| **Structured outputs** validated by Pydantic (D18) | Free-text parsing, regex over prose | Schema guarantees fields; mismatch is a clear error | Schemas must stay simple; citations feature cannot be combined, so evidence is our own field |
| **Verbatim evidence check + second read** instead of model confidence (PLAN §6; D72, D73) | Trust the model's confidence score | A quote either is or is not in the document; two reads either agree or not | Extra model calls per reply; many raised points, hence low flag precision |
| **Haiku as default** model (PLAN §5) | Sonnet or Opus for every step | The whole project fits a ~$10 app budget; evals decide where to step up; `LEVEL_VISION_MODEL` can raise only photos | Run-to-run variation on hard reads; more prompt work |
| **Tool-loop agents with narrow tools** (D30, D83) | Free-form SQL or code execution by the model | Read-only SQL over documented views; scenarios as parameters; chat cannot change data | Some questions need several tool calls; scope choices vary by wording (M3) |
| **Generated dataset with ground truth and seeds** (DATASET.md; D49) | Hand-labelled real quotes | Exact answer key; regenerate to prove nothing is hardcoded; traps by design | Generated documents are cleaner than real ones; the real-file set was never made |
| **Optional vendor link** (PLAN §8a) | Mandatory portal or template | Vendors will not use a portal; email stays complete | Two paths to support; uptake unknown; it removes follow-ups, not the first reply |
| **PyMuPDF** for PDFs (D37, D176) | Playwright, WeasyPrint, ReportLab | Already a dependency; pip-only; works on Windows | Limited HTML/CSS layout |
| **Render/Railway single instance with a disk** (DEPLOY.md; D115, D122) | Serverless | SQLite and uploads need a persistent disk; worker needs a long-lived process | No horizontal scale; paid instance for a disk; container runs as root |

---

## 10. How to run a demo

### Prepare (once, on the host)

1. Set `LEVEL_ANTHROPIC_API_KEY` and `LEVEL_PASSCODE`.
2. Open the app, enter the passcode.
3. **Overview → Demo data**, type `RESET`, click **Reload and read every reply**. Watch the Inbox; about 5 minutes.
4. When every reply is read, type `RESET` again and click **Save current data as the copy**.

Locally the same is: `uv run python -m level.db reset`, `uv run python scripts/seed.py --seed A`, `uv run python scripts/read_inbox.py`.

Before each demo: **Overview → Demo data → RESET → Restore saved copy** (instant, no model calls).

### The arc (about 12 minutes, PLAN §13)

1. **RFx** (2 min): open Past events, **Start from this event**, ask the co-pilot for a change ("add a required FSC question, 60-day payment"), show the email preview and attachments.
2. **Inbox** (2 min): five replies already read. Click **Read again** on the photo reply and watch the stages.
3. **Review** (3 min): on Mahalaxmi's line 30, show the price check, then **Accept all 5** → **Accept these 5 prices** → **Undo**. On Bharat's line 13 (≈ 90× the median), **Ask Bharat Corrugators**. Then open a vendor link from **Inbox → Sent → Vendor reply links (each opens only that vendor's own page)** in a private window and walk the vendor flow.
4. **Compare** (1 min): click a USD price and walk the receipt: as quoted → rate → per piece → delivered.
5. **Analyst** (4 min): where we stand; single vendor vs cheapest per line; only vendors that meet the requirements; cap at two; why one price is low; dollar at 90; which numbers are least sure; on-time delivery (declined).
6. **Award** (1 min): save two options from the Analyst, pick one under **Pick the award option to award**, **Award to these vendors**, open **Award memo (Word)** page one.

Have the Evals page and `DECISIONS.md` ready for "is this real?". To prove nothing is hardcoded, regenerate with a new seed (section 11).

---

## 11. How to extend

### A new category

Nothing in `level/` assumes a category. To run, say, laptops:

1. Create the RFx on the RFx page (or by chat).
2. For each line give a unit (`each`, `set`), an annual quantity, a spec as `name=value` pairs, and conversion factors only if vendors may quote in another unit (`pieces_per_pack=12`, `g_per_piece=412`; any `<unit>_per_<unit>` key works).
3. Give required questions a pass rule.
4. Send, then add replies with **Add a reply**.

Limits: unit words outside the built-in graph ("box", "roll") are assumed to be one of the line's unit and shown as Assumed, then caught by the price check if far off (D57). Freight terms that name places must use the RFx's own plant names.

### A new seed

```
uv run python scripts/generate_dataset.py --seed 7
uv run python -m level.db reset
uv run python scripts/seed.py --seed 7
uv run python scripts/read_inbox.py
uv run python -m evals.run --seed 7 --allow-unseen
```

Any name works (`7`, `C`, `demo2`). Do not commit seeds other than A. Seed B and the held-out vendor are reserved for QA.

To change the dataset itself (vendors, traps, styles), edit `scripts/dataset/` and `scripts/generate_dataset.py`; `tests/test_dataset.py` checks the designed outcomes.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| RFx | Request for quotation (or proposal, or information): what the buyer sends |
| Line | One item in the RFx, with a unit and annual quantity |
| Bid levelling | Putting every quote on one basis; the origin of the name Level |
| Verified / Assumed / Check / Mismatch / Not quoted / Awaiting reply | Buyer-facing price statuses. Code calls them confirmed / inferred / needs_review / conflict / not_quoted / awaiting. "Not read yet" is Awaiting for a reply that arrived but has not been read |
| Issue / point | Something a person should look at. Code calls it a flag |
| Price check | The buyer-only panel on a Review price issue: own last-year record, median of other quotes, last year's contract, other vendors this year |
| Receipt | The panel behind a price: vendor's words, source, conversion steps, checks |
| Award option | One award calculation with its settings. Code calls it a scenario |
| Requirements | Required questionnaire questions with pass rules. Code calls them gating questions |
| Delivered price | Price plus freight. Code calls it landed |
| L1 / L2 / L3 | Lowest, second and third eligible price on a line |
| Ex-works / ex-GST | Before freight / before tax |
| Reference price | A price taken from the vendor's own record last year ("same as last year") |
| Seed | A generated version of the dataset; A is for tuning, B and held-out are unseen |
| Trap | A deliberately planted difficulty in the dataset (X01–X26) |
| Silent error | A wrong price shown as Verified |
| Flag precision | Share of raised issues that are real |
| Claim | Internal: one thing a vendor said about price, with evidence |
| Cell | Internal: one vendor × line price in the grid |
