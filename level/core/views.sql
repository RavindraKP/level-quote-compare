-- Read-only views the analyst agent queries (PLAN §7). Money columns are TEXT decimal
-- strings in the RFx base currency (INR), ex-GST, per the line's canonical unit.
-- Installed by level/core/views.py; every statement ends with a double semicolon.

-- v_lines: one row per RFx line. annual_qty in canonical_uom; last_price_inr per canonical_uom
-- is last period's contract price (what the buyer paid); incumbent_vendor supplied it.
DROP VIEW IF EXISTS v_lines;;
CREATE VIEW v_lines AS
SELECT l.id AS line_id, l.rfx_id, l.line_no, l.description, l.spec, l.canonical_uom,
       l.annual_qty, l.plant, l.last_price AS last_price_inr,
       l.incumbent_vendor_id, iv.name AS incumbent_vendor
FROM rfx_line l LEFT JOIN vendor iv ON iv.id = l.incumbent_vendor_id;;

-- v_vendors: invited vendors with reply status (replied / invited / no_response).
DROP VIEW IF EXISTS v_vendors;;
CREATE VIEW v_vendors AS
SELECT v.id AS vendor_id, i.rfx_id, v.name, v.is_incumbent, i.status AS reply_status
FROM vendor v JOIN invitation i ON i.vendor_id = v.id;;

-- v_cells: the comparison grid. state is confirmed / inferred / needs_review / conflict /
-- not_quoted / awaiting. unit_price_inr is ex-works; landed_price_inr = unit price + freight
-- (NULL when unknown). Payment-terms carrying cost is not in landed: it is an explicit scenario
-- option (ScenarioParams.include_payment_terms, DECISIONS WP6). line_value_inr = unit price x
-- annual qty.
DROP VIEW IF EXISTS v_cells;;
CREATE VIEW v_cells AS
SELECT c.rfx_id, c.vendor_id, v.name AS vendor, c.rfx_line_id AS line_id, l.line_no,
       l.description, c.state, c.reason, c.as_quoted, c.unit_price_inr, c.landed_price_inr,
       CAST(c.unit_price_inr AS REAL) * CAST(l.annual_qty AS REAL) AS line_value_inr,
       c.confirmed_by, c.assumptions
FROM cell c JOIN vendor v ON v.id = c.vendor_id JOIN rfx_line l ON l.id = c.rfx_line_id;;

-- v_terms: vendor-level commercial terms as written (discount, freight, payment, validity,
-- tax, tooling, moq, lead_time, other). is_conditional = 1 for conditional discounts.
DROP VIEW IF EXISTS v_terms;;
CREATE VIEW v_terms AS
SELECT t.rfx_id, t.vendor_id, v.name AS vendor, t.type, t.text, t.value, t.conditions,
       t.is_conditional, t.evidence_quote, t.evidence_locator
FROM vendor_term t JOIN vendor v ON v.id = t.vendor_id;;

-- v_questionnaire: each vendor's answer to each question, with the code-decided verdict
-- (pass / fail / unclear). Missing answers appear with verdict 'unclear'.
DROP VIEW IF EXISTS v_questionnaire;;
CREATE VIEW v_questionnaire AS
SELECT q.rfx_id, q.id AS question_id, q.q_no, q.section, q.text AS question, q.is_gating,
       v.id AS vendor_id, v.name AS vendor, a.value AS answer,
       COALESCE(a.verdict, 'unclear') AS verdict, a.rationale
FROM question q JOIN invitation i ON i.rfx_id = q.rfx_id JOIN vendor v ON v.id = i.vendor_id
LEFT JOIN answer a ON a.question_id = q.id AND a.vendor_id = v.id;;

-- v_flags: everything a human should look at. state open / closed; closed_by checks /
-- buyer / vendor.
DROP VIEW IF EXISTS v_flags;;
CREATE VIEW v_flags AS
SELECT f.id AS flag_id, f.rfx_id, f.vendor_id, v.name AS vendor, f.rfx_line_id AS line_id,
       l.line_no, f.type, f.severity, f.reason, f.state, f.closed_by, f.resolution
FROM flag f LEFT JOIN vendor v ON v.id = f.vendor_id LEFT JOIN rfx_line l ON l.id = f.rfx_line_id;;

-- v_history: last period's records per vendor and line (the buyer's price memory).
-- source = 'contract' (what the buyer paid that vendor; one per line) or 'quote' (a bid that
-- did not win). "What we paid" and "who supplied us" use source = 'contract' only.
DROP VIEW IF EXISTS v_history;;
CREATE VIEW v_history AS
SELECT h.vendor_id, v.name AS vendor, h.rfx_line_id AS line_id, l.line_no, l.rfx_id, h.period,
       h.source, h.unit_price, h.currency, h.uom
FROM price_history h JOIN vendor v ON v.id = h.vendor_id LEFT JOIN rfx_line l ON l.id = h.rfx_line_id;;
