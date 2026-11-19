# Level: one-page note

**Kill the quote spreadsheet.** Level drafts an RFx with a co-pilot, reads whatever vendors send back (Excel, PDF, Word, a phone photo, a three-line email), lines every quote up on the same lines, units and currency, and lets the buyer ask questions in plain language, through to a defensible award.

**Would a buyer with ₹4 crore on the line act on it?** Yes, as a decision aid with review done: every number is traceable, every doubt is shown, and the remaining risk is stated in rupees.

## What I decided, and why

1. **Reading is the model's job; arithmetic is code's job.** The model copies what the vendor wrote and proposes the matching line. Every conversion, FX rate, discount, total and award is tested code.
2. **No number without a receipt.** Every price carries the vendor's own words. Code checks those words exist in the source, or the price is never shown as verified. One click shows every step behind it.
3. **Unknown stays unknown.** A missing line is "not quoted", never ₹0 or an average. "Freight extra" makes the delivered price unknown, not free.
4. **Status comes from checks, never from the model's confidence.** Doubt is shown in words ("41.50 or 44.50?"), and only the doubtful points reach a person.
5. **What-ifs are inputs, not edits.** "Dollar at 90" or "only vendors who cleared the questionnaire" feed a tested award calculator; chat can never change the data.

## Trust, in rupees

Page one of the award memo says how much of the award rests on prices nobody has verified yet. For the VP's split award (₹5.08 Cr, 5.3% below last year): **15 unverified prices worth ₹99.9 lakh a year; if each were 10% off, the total moves by up to ₹10.0 lakh, and the winner changes on 21 lines.** The buyer can award now, or ask those vendors first.

## How it measures up

Scored against an answer key on five vendors, 30 lines and 26 planted ugly edges, plus quotes and a vendor layout never seen while building (proof nothing is hardcoded):

- **Wrong prices shown as verified: 0** on every dataset.
- **Ugly edges handled: 100%** on the demo data, the unseen quotes and the new layout.
- **When unsure, it asks:** about 1 in 5 prices go to review, about 6 in 10 of those are real problems, and none of the real problems slipped through.
- **The VP's questions answered correctly: 90–95%**, and the same answer however a question is worded.
- **Inbox to side-by-side comparison: under 5 minutes**, against "three days gone".
- **Below target:** questions worth the buyer's time are 60–67% (target 70%), and they vary between runs on photos and scans. It asks more than it should; that is the next fix.

## What I deliberately left out

Real email, logins, ERP and purchase orders, negotiation and auctions, a live FX feed (a pinned, sourced rate is easier to defend) and approval workflows: plumbing that teaches nothing about the hard part, which is what arrives. Every run used the lowest-cost model, about $10 for the whole build.

## The better problem

Reading has a ceiling. "Per box" of how many? "Freight extra" of how much? No reader can extract what the vendor never wrote. Of 33 points the replies raised, **30 could only be settled by the vendor**.

So the fix is upstream: each vendor gets an optional personal link. They drop in any file, a co-pilot asks only the questions code raised about their own quote, and confirmed prices arrive already verified. A vendor never sees rivals, rankings or price feedback, and email still works.
