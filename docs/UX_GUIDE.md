# UX and writing guide

Every screen, message and email in Level follows this guide. The test for each one: would a category buyer at a large Indian manufacturer mistake this for the procurement portal their company already pays for? It should look calm, dense with useful information, and never show the machinery underneath.

## 1. Vendor emails

Drafted emails are read by a vendor's sales person, usually on a phone. Today's drafts repeat the same issue many times and use internal terms. Fix both in code before the model writes anything.

### How a draft is built

1. **Collect** all open flags for one vendor that only the vendor can close.
2. **Group in code** by root cause, not by line. One unit question covering 13 lines is one question that lists 13 lines. Group keys: same flag type + same source snippet, or same flag type + same missing field.
3. **Order** by money at stake, largest first. At most 5 questions in one email; the rest wait for the next round.
4. **Write** with the model, given only the grouped list, the vendor's name and the RFx title. The model writes the wording; it does not decide what to ask.
5. **Check in code**: each group appears exactly once; no internal term from the banned list below appears; every line number mentioned exists in the RFx.
6. **Show** to the buyer as an editable draft. Nothing is sent without a click.

### Shape of every email

```
Subject: <RFx title> — <n> points to confirm on your quotation

Dear <contact name or "Sir/Madam">,

Thank you for your quotation dated <date>. Before we complete our evaluation, please help us with the following:

1. <Question in plain words, quoting their own wording where useful.>
   Lines: 12, 14, 19
2. ...

A short reply to this email is enough; there is no need to resend the full quotation.

Regards,
<buyer name>
<company>
```

### Rules
- Quote the vendor's own words: "Your rate card says ₹310 per box. How many partition sets are in one box?"
- Ask, never accuse. No "error", "mismatch", "discrepancy", "invalid", "failed".
- One idea per question. Line numbers on their own line, grouped.
- No mention of other vendors, rankings, targets or last year's prices from anyone else.
- "Same as last year" confirmations list the lines and prices taken: "We have taken your FY25 prices for lines 26 to 30, as listed below. Please confirm these still apply."
- Indian business register: courteous, direct, short. No marketing tone.

## 2. Words on screen

### Say this, not that

| Internal term (never shown) | Shown to the buyer |
|---|---|
| flag | issue / point to check |
| cell state: Confirmed / Inferred / Needs review / Conflict / Not quoted / Awaiting | Verified / Assumed / Check / Mismatch / Not quoted / Awaiting reply |
| claim, extraction run, block, locator | (do not show) or "source" |
| evidence quote | "Vendor wrote:" |
| plausibility check failed | "Much higher than other quotes for this item" |
| no_history | "Vendor referred to last year, but did not supply this item then" |
| read_mismatch | "Hard to read in the photo: 41.50 or 44.50?" |
| gating fail | "Does not meet: ISO 9001 certificate expired on 31 Aug 2026" |
| scenario | "award option" |
| canonical unit | "per piece" (use the actual unit name) |

Rules:
- Every issue says what is wrong, why it matters, and what to do, in one line each at most.
- Buttons say what happens, and name the amount when there is one: "Accept ₹42.12 (last year's contract price)", "Ask Mahalaxmi Packaging", "Mark line 6 as not quoted", "Award to these vendors". Never "Submit" or "OK".
- No apologies, no exclamation marks, no emoji.
- Sentence case for headings and buttons.

### Numbers, dates, money
- Indian grouping: ₹1,23,45,678. Large totals as ₹3.71 Cr or ₹48.2 L with the exact figure on hover or in the receipt.
- Unit prices to 2 decimals, totals to whole rupees.
- Always state the basis next to a price: "per piece, ex-GST, delivered".
- Dates as 17 Nov 2026; times as 2:10 PM IST.
- Percentages to one decimal.
- Right-align numbers; tabular figures.

### Empty, loading and error states
- Empty: say what will appear and how to make it appear. "No replies yet. Replies appear here as vendors respond; you can also add one yourself."
- Loading: name the stage. "Reading Kaveri's Excel: matching 34 rows to your 30 lines."
- Error: what happened and the next step. "This PDF is password-protected. Ask the vendor for an unlocked copy, or upload a photo of the pages." Never a stack trace.

## 3. Look and layout

Streamlit, themed so it reads as a product, not a notebook.

### Theme
- `.streamlit/config.toml`: light theme; one primary colour (deep teal or indigo, not Streamlit red); neutral grey background for the sidebar; Inter font.
- A small `app/components/style.py` that injects one block of CSS: tighter padding, card style for summary tiles, status chip styles, table header style. No per-page CSS.
- Hide Streamlit's default menu, footer and "deploy" button.

### Every page has the same frame
- **Event header** at the top: event name and number (e.g. RFX-2026-014), status chip, response deadline with time remaining, buyer name.
- **Stage stepper** under it: Draft → Issued → Collecting → Evaluate → Awarded, current stage highlighted. Clicking a stage goes to its page.
- **Sidebar**: navigation grouped as Sourcing (RFx, Inbox), Evaluation (Review, Compare, Analyst), Decision (Award), Admin (Evals). A badge on Review shows the number of open issues.

### Patterns from real procurement portals
| Pattern | Where |
|---|---|
| **Response tracker**: one row per vendor with status (invited, viewed, replied, no response), received time, lines quoted (27/30), issues open, questionnaire result | Inbox, top |
| **L1 / L2 / L3 ranking** per line among eligible vendors, the convention Indian buyers use | Compare grid, small rank tag in each cell |
| **Bid summary tiles**: total by vendor, lines quoted, eligibility, open issues | Compare, above the grid |
| **Technical vs commercial split**: questionnaire and documents (technical) on one tab, prices (commercial) on another | Compare |
| **Supplier card**: name, incumbent badge, certificates with expiry, last year's spend | Click on a vendor name |
| **Audit trail**: who changed what and when, filterable | Award page and receipt |
| **Approval-ready summary**: recommendation, savings, risks, on one screen before Award | Award |

### The grid
- Lines as rows, vendors as columns; line description and annual quantity frozen on the left.
- One cell shows price, rank tag (L1/L2/L3) and a status icon. Lowest eligible price per line in bold.
- Status uses icon + colour + text in the tooltip, never colour alone.
- Ineligible vendors' columns greyed with the reason in the header.
- Footer rows: total, lines quoted, open issues.

### Review queue
Built so a first-time buyer can clear it without help. Each rule is a pass/fail check.

**Issue list (left)**
- R1. One heading per vendor: the full name (never truncated), the open count and the money at stake ("Mahalaxmi Packaging · 8 open · ₹2.06 Cr at stake").
- R2. Vendors are ordered by money at stake, and issues within a vendor by money at stake; points with no line (no money attached) come last within their vendor. Money at stake = annual quantity × the price held, computed in `level/core/review.py`.
- R3. Each row is one plain line that names the line and the value: "Line 6 · ₹42.12 taken from last year's contract", "Line 13 · ₹328.00 is 89.6× the median of other quotes (₹3.66)". A row never says only the issue type ("Information missing"); points with no line say what is missing ("Delivered price unknown: freight amount not stated").
- R4. Filters for priority and issue type above the list; a "Show decided" toggle. Decided issues carry a ✓ and stay in their place.

**Focus card (right)**
- R5. Progress with Previous / Next: "4 of 32 · 28 left". The total counts open and decided issues in the current filter; "left" counts open ones.
- R6. Heading: issue type, priority chip, vendor, line number and description, money at stake.
- R7. "What we found" in one sentence that adds to the heading, never repeats it. "Why it matters" in one muted line.
- R8. "Evidence": the vendor's exact words, with where they are. The file viewer appears only when a stored file exists; for a reply in an email body, the email text (snippet marked) is behind "Show their whole email". No "file not stored" box. The vendor's words appear once on the card.
- R9. Every price issue (an issue on one line's price) shows a "Price check", marked "For you only. Vendors never see this.": this vendor's record for the line last year (price, contract or losing quote, and the date from the past event), the other vendors' prices this year with their status, the median of the other vendors' prices, and last year's contract price from any supplier. All figures come from SQLite and `level/core/`; none reach a vendor (email, Excel template, reply link).

**Actions**
- R10. Every button names its outcome, and the amount when there is one: "Accept ₹42.12 (last year's contract price)", "Accept ₹27.99 as read", "Use 41.50 (as written)", "Enter a different price" (then "Save this price for line 6"), "Mark line 6 as not quoted", "Ask Mahalaxmi Packaging". No bare "Accept price", "OK" or "Submit".
- R11. "Ask <vendor>" builds the vendor's one grouped email and shows it under the buttons, saying which question carries this point ("This point is question 3 in the email"). Points only the buyer can decide say so instead.
- R12. After any decision the next open issue opens. The last decision is shown in a bar above the filters with "Undo", which reopens it through the same audit path (a bulk accept is undone as one).
- R13. Same cause, one action: when two or more open issues for one vendor have the same issue type and the same accept (for example five "price taken from last year" from one sentence, or three "one box taken as one piece"), the card offers "Accept all N". It opens a confirmation listing every line, description and value; nothing is saved until "Accept these N prices".
- R14. Chat, bulk accept and undo never change extracted data: they close or reopen issues; prices are rebuilt by code.

**Header**
- R15. The reply date is shown relative to today: "Replies due 1 Mar 2025 · closes in 2 days", "Replies closed 24 Feb 2025 · 3 days ago", so an old dataset date does not look like a bug.
- Keyboard shortcuts are not available in Streamlit; buttons are full-width and named instead.

### Analyst
- Suggested questions as chips when the thread is empty.
- Answer first, then table or chart, then a collapsed "How this was worked out".
- Caveats in a muted box under the answer, not mixed into the text.

## 4. Quality bar before any screen is called done
- Read every string on the page aloud: would a buyer say it that way?
- No internal term from section 2 visible anywhere, including tooltips and exports.
- Every number has a unit and a basis.
- Empty, loading and error states seen in a browser.
- Screenshot at 1440px and 1280px wide.
