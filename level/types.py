"""Shared contracts between packages.

Two kinds of model live here:

1. Model-facing schemas (ExtractionOutput, MatchOutput and their parts). These are sent
   to Claude as structured-output schemas, so they hold only what the model may say:
   transcriptions and mappings, never computed numbers. Prices are strings exactly as
   written ("1,250.00", "Rs 42/-"); code parses them.
2. Domain records (QuoteClaim, VendorTerm, Cell, Flag, Receipt, Scenario*, Job). These are
   what code stores, computes and shows. Money is Decimal, never float.

If a contract must change, change it here and note it in DECISIONS.md.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------


class CellState(StrEnum):
    """One state per vendor x line. Applied by code in PLAN §6 order."""

    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    NEEDS_REVIEW = "needs_review"
    CONFLICT = "conflict"
    NOT_QUOTED = "not_quoted"
    AWAITING = "awaiting"


class ClosedBy(StrEnum):
    """Who confirmed a cell or closed a flag. Vendor is the strongest."""

    CHECKS = "checks"
    BUYER = "buyer"
    VENDOR = "vendor"


class FlagType(StrEnum):
    """The flag catalogue in PLAN §6."""

    EVIDENCE = "evidence"
    READ_MISMATCH = "read_mismatch"
    ARITHMETIC = "arithmetic"
    PLAUSIBILITY = "plausibility"
    SPEC_DEVIATION = "spec_deviation"
    ATTACHMENT_CONFLICT = "attachment_conflict"
    MISSING_INFO = "missing_info"
    REFERENCE = "reference"
    NO_HISTORY = "no_history"
    SUSPICIOUS_INSTRUCTION = "suspicious_instruction"
    UNREADABLE = "unreadable"  # file could not be read (password, corrupt, empty)


# Which cell state an open flag of each type pushes a cell to (PLAN §6 table).
FLAG_CELL_EFFECT: dict[FlagType, CellState | None] = {
    FlagType.EVIDENCE: CellState.NEEDS_REVIEW,
    FlagType.READ_MISMATCH: CellState.NEEDS_REVIEW,
    FlagType.ARITHMETIC: CellState.CONFLICT,
    FlagType.PLAUSIBILITY: CellState.NEEDS_REVIEW,
    FlagType.SPEC_DEVIATION: CellState.CONFLICT,
    FlagType.ATTACHMENT_CONFLICT: None,  # affects the questionnaire answer, not a price cell
    FlagType.MISSING_INFO: CellState.INFERRED,
    FlagType.REFERENCE: CellState.INFERRED,
    FlagType.NO_HISTORY: CellState.NOT_QUOTED,
    FlagType.SUSPICIOUS_INSTRUCTION: None,
    FlagType.UNREADABLE: None,
}


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


TermType = Literal[
    "discount", "freight", "payment", "validity", "tax", "tooling", "moq", "lead_time", "other"
]
ClaimScope = Literal["line", "rule", "reference"]

# ---------------------------------------------------------------------------
# Model-facing schemas: extraction (stage 1)
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Base for model-facing schemas: unknown keys are an error, not silently kept."""

    model_config = ConfigDict(extra="forbid")


class SourceRef(_Strict):
    """Where a value came from. The quote must be verbatim; code checks it (T1)."""

    locator: str = Field(
        description="Where in the source: 'Sheet2!D14', 'page 2', 'paragraph 7', "
        "'table 1, row 3', 'email, line 2' or 'image'."
    )
    quote: str = Field(
        description="The exact words from the source, copied character for character."
    )


class Basis(_Strict):
    """What one quoted price buys, as written. 'per 1000 pieces' -> per='piece', multiple='1000'."""

    per: str = Field(
        description="The unit the price is for, as written: 'piece', 'pack', 'dozen', 'hour'."
    )
    multiple: str | None = Field(
        description="How many of that unit the price covers, as written ('1000' in "
        "'per 1000 pcs'). Null when the price is for one unit."
    )


class QuantityBreak(_Strict):
    """A volume slab as written. Code decides which slab applies."""

    text: str = Field(description="The slab exactly as written, e.g. 'above 50,000 pcs'.")
    min_qty: str | None = Field(description="Lower bound as written, or null.")
    max_qty: str | None = Field(description="Upper bound as written, or null.")


class ExtractedRow(_Strict):
    """One priced row exactly as the vendor wrote it. No conversion, no arithmetic."""

    item_text: str = Field(description="The vendor's own description or code for the item.")
    price: str | None = Field(description="The price exactly as written, or null if absent.")
    currency: str | None = Field(
        description="Currency as written or clearly stated nearby, or null."
    )
    uom_text: str | None = Field(description="Unit-of-measure text exactly as written, or null.")
    basis: Basis | None
    quantity_break: QuantityBreak | None
    quantity: str | None = Field(
        description="A quantity written on the same row (e.g. order qty), as written, or null."
    )
    amount: str | None = Field(
        description="A line amount/total written on the same row, as written, or null."
    )
    evidence: SourceRef


class StatedTotal(_Strict):
    """A total the vendor wrote (grand total, sub-total). Code checks it against the rows."""

    label: str = Field(description="The label as written, e.g. 'Grand Total'.")
    amount: str = Field(description="The total exactly as written.")
    evidence: SourceRef


class ExtractedTerm(_Strict):
    """A document-level commercial term (discount, freight, payment, validity...)."""

    type: TermType
    text: str = Field(description="The term in the vendor's words.")
    value: str | None = Field(description="The number or period as written, e.g. '2%', '60 days'.")
    conditions: str | None = Field(description="Any condition attached, as written, or null.")
    evidence: SourceRef


class ExtractedAnswer(_Strict):
    """A questionnaire answer as given by the vendor."""

    question_ref: str = Field(description="The question number or text the answer responds to.")
    answer_text: str
    stance: Literal["yes", "no", "unclear"] = Field(
        description="If the question asks yes or no: 'yes' when the vendor's words say they do "
                    "or have it (with or without the word yes), 'no' when they say they do "
                    "not, 'unclear' when they hedge or answer something else. Other "
                    "questions: 'unclear'.")
    evidence: SourceRef


class Ambiguity(_Strict):
    text: str = Field(description="What is unclear and why.")
    locator: str | None


class SuspiciousInstruction(_Strict):
    """Text in a vendor document addressed to the reader or a system. Never followed."""

    text: str
    locator: str | None


class ExtractionOutput(_Strict):
    """Structured output of the extraction stage for one document."""

    document_kind: Literal["quote", "certificate", "test_report", "other"] = Field(
        description="What this document mainly is."
    )
    rows: list[ExtractedRow]
    stated_totals: list[StatedTotal]
    terms: list[ExtractedTerm]
    questionnaire_answers: list[ExtractedAnswer]
    ambiguities: list[Ambiguity]
    suspicious_instructions: list[SuspiciousInstruction]
    hard_to_read: list[str] = Field(
        description="Regions or values that were hard to read (photos and scans). Empty if none."
    )


class DocumentFacts(_Strict):
    """Smaller schema for certificates and reports (PLAN §5 'attachments are evidence')."""

    document_type: str = Field(description="e.g. 'ISO 9001 certificate', 'board test report'.")
    standard: str | None = Field(description="Standard or scheme named, as written, or null.")
    issuer: str | None
    holder: str | None = Field(description="Company the document is issued to, as written.")
    scope: str | None
    issue_date: str | None = Field(description="Issue date exactly as written, or null.")
    expiry_date: str | None = Field(description="Expiry / valid-until date as written, or null.")
    evidence: SourceRef


class ReadDisagreement(_Strict):
    """One place where the second reader sees something different from the extraction."""

    row_index: int | None = Field(description="Index of the extracted row, or null for terms.")
    field: str = Field(description="Which field differs: 'price', 'uom_text', 'item_text'...")
    extracted_value: str | None
    source_value: str | None = Field(description="What the source actually says, as written.")
    quote: str = Field(description="The exact source words that show the difference.")


class SecondReadOutput(_Strict):
    """T2: an independent check of an extraction against the source."""

    disagreements: list[ReadDisagreement]
    missed_rows: list[str] = Field(
        description="Priced items in the source that the extraction left out, as written."
    )


class ZoomValue(_Strict):
    item_text: str = Field(description="The item exactly as given in the request.")
    price: str | None = Field(description="The price as written in the image, or null.")
    legible: bool = Field(description="False if the digits cannot be read with certainty.")
    quote: str


class ZoomReadOutput(_Strict):
    values: list[ZoomValue]


# ---------------------------------------------------------------------------
# Model-facing schemas: matching (stage 2)
# ---------------------------------------------------------------------------


class SpecDeviation(_Strict):
    attribute: str = Field(description="The spec attribute that differs, named as in the RFx line.")
    rfx_value: str
    quoted_value: str


class RowMatch(_Strict):
    """Mapping of one extracted row to RFx lines."""

    row_index: int = Field(description="Index of the row in the extraction output.")
    scope: ClaimScope | Literal["none"] = Field(
        description="'line' for a specific line, 'rule' for a price that covers a group of lines, "
        "'reference' for a row that points to an earlier price instead of giving one, "
        "'none' if no line matches."
    )
    rfx_line_nos: list[int] = Field(description="RFx line numbers this row applies to.")
    rule_text: str | None = Field(description="For rule or reference scope: the rule as written.")
    rationale: str
    spec_deviations: list[SpecDeviation]


class MatchOutput(_Strict):
    matches: list[RowMatch]


# ---------------------------------------------------------------------------
# Domain records (stored and computed by code)
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """A receipt anchor: which file, where in it, and the verbatim words."""

    attachment_id: int | None = None
    locator: str
    quote: str
    verified: bool | None = None  # result of the T1 evidence check; None = not yet checked


class QuoteClaim(BaseModel):
    """One thing a vendor said about price, as quoted. Never edited by chat."""

    id: int | None = None
    rfx_id: int
    vendor_id: int
    run_id: int | None = None
    vendor_item_text: str
    raw_price: str | None
    raw_currency: str | None
    raw_uom_text: str | None
    basis: Basis | None = None
    quantity_break: QuantityBreak | None = None
    scope: ClaimScope = "line"
    rule_text: str | None = None
    evidence: Evidence
    read_candidates: list[str] = []  # differing values from independent reads (photos, scans)


class VendorTerm(BaseModel):
    id: int | None = None
    rfx_id: int
    vendor_id: int
    run_id: int | None = None
    type: TermType
    value: str | None
    conditions: str | None = None
    is_conditional: bool = False  # conditional discounts are shown, not applied by default
    evidence: Evidence


class ConversionStep(BaseModel):
    """One step in the chain from as-quoted to the line's canonical unit and INR."""

    label: str  # e.g. "per 1000 pieces -> per piece"
    operation: str  # e.g. "divide by 1000"
    input: Decimal
    factor: Decimal
    output: Decimal
    source: str | None = None  # where the factor came from: "line.conversion_factors", "fx.USDINR"


class Cell(BaseModel):
    """One vendor x line in the comparison grid. Rebuilt by code only."""

    rfx_id: int
    vendor_id: int
    rfx_line_id: int
    state: CellState
    reason: str  # plain-text reason for the state, always shown (colour is never the only signal)
    claim_id: int | None = None
    as_quoted: str | None = None  # e.g. "Rs 42/kg"
    unit_price_inr: Decimal | None = None
    landed_price_inr: Decimal | None = None
    conversion_steps: list[ConversionStep] = []
    assumptions: dict[str, str] = {}  # assumption key -> value used
    flag_ids: list[int] = []
    confirmed_by: ClosedBy | None = None


class Flag(BaseModel):
    """Anything a human should see. Opened by code; the model can never close one."""

    id: int | None = None
    rfx_id: int
    vendor_id: int | None
    target_kind: Literal["claim", "term", "answer", "cell", "attachment", "message"]
    target_id: int | None
    rfx_line_id: int | None = None
    type: FlagType
    severity: Severity
    reason: str
    suggested_action: str | None = None
    candidates: list[str] = []  # both values for read_mismatch
    state: Literal["open", "closed"] = "open"
    closed_by: ClosedBy | None = None
    resolution: str | None = None


class CheckResult(BaseModel):
    name: str  # "T1 evidence", "T4 plausibility", ...
    passed: bool
    detail: str


class AuditEntry(BaseModel):
    actor: str  # "system", "buyer", "vendor"
    action: str
    before: dict | None = None
    after: dict | None = None
    at: str


class Receipt(BaseModel):
    """Everything behind one cell: what was said, where, how it was converted, what was checked."""

    cell: Cell
    claim: QuoteClaim | None
    evidence: Evidence | None
    conversion_steps: list[ConversionStep] = []
    assumptions_used: dict[str, str] = {}
    checks: list[CheckResult] = []
    flags: list[Flag] = []
    history: list[AuditEntry] = []


class ScenarioParams(BaseModel):
    """An award what-if. Chat changes these, never the extracted data."""

    strategy: Literal["single_vendor", "cheapest_per_line", "capped_vendors"]
    single_vendor_id: int | None = None
    vendor_ids: list[int] | None = None  # None = every vendor with a reply
    require_gating_pass: bool = True  # exclude vendors that failed a gating question
    exclude_unclear_gating: bool = False  # also exclude vendors whose gating is unclear
    max_vendors: int | None = None
    excluded_line_ids: list[int] = []
    price_basis: Literal["ex_works", "landed"] = "landed"
    # Landed = price + freight. The payment-terms carrying cost is added only when asked.
    include_payment_terms: bool = False
    fx_override: dict[str, Decimal] = {}  # e.g. {"USDINR": Decimal("90")}
    apply_conditional_discounts: bool = False
    unconfirmed: Literal["include", "exclude", "pessimistic"] = "include"


class Allocation(BaseModel):
    rfx_line_id: int
    vendor_id: int | None  # None = no eligible price for this line
    unit_price_inr: Decimal | None
    annual_qty: Decimal
    line_total_inr: Decimal | None
    cell_state: CellState | None


class SensitivityItem(BaseModel):
    rfx_line_id: int
    vendor_id: int
    winner_changes: bool  # does the winner change if this unconfirmed cell moves +/-10%?
    exposure_inr: Decimal


class ScenarioResult(BaseModel):
    params: ScenarioParams
    allocation: list[Allocation]
    vendors_used: list[int]
    total_inr: Decimal | None  # None when no line has a price on the chosen basis (never 0)
    last_year_total_inr: Decimal | None
    savings_inr: Decimal | None
    savings_pct: Decimal | None
    coverage_gaps: list[int]  # rfx_line_ids with no eligible price
    caveats: list[str]  # attached by code, rendered whether or not the model mentions them
    sensitivity: list[SensitivityItem]


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(BaseModel):
    id: int
    kind: str
    target: str
    status: JobStatus
    stage: str | None
    progress: float  # 0.0 to 1.0
    attempts: int
    error: str | None
    heartbeat_at: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
