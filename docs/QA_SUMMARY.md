# QA summary

Level went through three QA passes, a full UX audit and a final scoring run. Each pass was done by a separate session that had not built the part it tested, in a real browser (Playwright, 1440 and 1280 px wide), against seed A, with server logs checked for errors.

## What each pass checked

- **Hardcoding audit:** no fixture vendor name, file name, line number or demo question in any prompt or code path in `level/`.
- **Number audit:** every figure on the demo path traced back to the vendor's words and recomputed by hand.
- **Code-break sweep:** no traceback, raw JSON, SQL or code block on any buyer or vendor page.
- **The three co-pilots:** RFx co-pilot, analyst and vendor link, including attempts to make the vendor link leak other vendors' prices or rankings.
- **Clarification round trip:** buyer asks, vendor answers on their link, the answer flows back into Compare and the award.
- **Usability:** five checks on the Review queue at both widths, and every page against `docs/UX_GUIDE.md`.

## What it found and what happened

| Pass | Blockers found | Examples | Outcome |
|---|---|---|---|
| 1 | 4 | A demo phrase in a prompt; a delivered price shown as confirmed when freight was unknown | All fixed; evals extended to score delivered prices |
| 2 | 2 | No delivered price on any line totalled as ₹0; a fixture name in a code comment | Fixed |
| UX audit | 10 | Award page crash after vendor answers; raw data on screen; sending a new RFx moved the buyer off their event | All fixed (46 changes) |
| 3 | 1 | A vendor's freight answer for one plant applied to every plant | Fixed and covered by tests |

## Final state

- About 340 unit tests pass; lint is clean.
- Final scores on three datasets (demo, unseen quotes, a new vendor layout): **0 wrong prices shown as verified**, 100% of prices correct or marked for checking, 100% of the 26 planted ugly edges handled, 90–95% of the analyst questions answered correctly.
- One change that halved review items was **reverted** before submission because it lowered accuracy on unseen quotes; it was not tuned to the answer key.

## Known open issues

- **Too many review items:** 60–67% of them point at real problems, against a 70% target. The reader's own "unclear" notes and unit assumptions are the main source.
- **Photos and scans vary between runs:** the same photo can be read slightly differently each time, which changes the number of review items. Doubt is always shown; it never becomes a verified wrong price.
- **Synthetic data only:** every score is on generated documents. Real photos, scans and handwritten corrections are not yet measured.
- **Prototype scope:** no real email, logins, ERP or approvals; one process and one SQLite file.
