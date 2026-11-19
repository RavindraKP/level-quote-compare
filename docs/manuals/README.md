# Level manuals

| Document | For | What it covers |
|---|---|---|
| [BUYER_GUIDE.md](BUYER_GUIDE.md) | A procurement buyer using Level for the first time | Every page from signing in to the award, what each issue and price status means, FAQ and Windows notes |
| [VENDOR_GUIDE.md](VENDOR_GUIDE.md) | A supplier who received the RFx email | The email and attachments, replying in any format or through the personal link, what the buyer does and does not see |
| [PRODUCT_MANUAL.md](PRODUCT_MANUAL.md) | A product team taking Level over | Problem, story, feature inventory with status, the ten rules, eval scorecard, open issues, roadmap, trade-offs, demo, glossary |
| [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) | Engineers reviewing how the platform works | Architecture, data model, pipeline, trust layer, core, worker, agents, security, deployment, tests, measured latency, trade-offs, industry terms, moving to Postgres |

Screenshots are in `img/`. All but two were retaken on 19 Nov 2026 at `6680594` with Playwright (Chromium, 1440 px wide, one 1280 px Compare view) on seed A, after one reading of the inbox; the passcode screen and the Add a reply window are unchanged from the first edition. The vendor guide still uses the first page of the RFx PDF from `docs/qa/vendor-confirm/`.

The technical spec now also has §19 "In industry terms" (agentic AI, RAG, orchestration, guardrails, evals, observability mapped to Level's files, and when to adopt LangGraph, a vector database, Postgres or Langfuse) and §20 "Moving to Postgres".
