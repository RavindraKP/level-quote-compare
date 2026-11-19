"""RFx co-pilot: a tool-using agent that edits the draft in SQLite.

How a turn works:
1. The buyer's message is sent with a snapshot of the current draft (so direct edits made on
   the page are always seen) and the completeness issues found by code.
2. The model may call tools; each tool is a function in draft.py. We run them, send the
   results back, and repeat until the model answers in text (at most MAX_STEPS rounds).
3. The chat is saved in `thread` / `chat_message`, and every tool call in `tool_call`.

Every request goes through level/llm.py (the app's key, streaming, stop_reason checks).
The co-pilot cannot send the RFx: there is no tool for it. Sending is a button on the page.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import sqlalchemy as sa

from level import db, llm
from level.agents.copilot import draft

MAX_STEPS = 8

SYSTEM = """You help a procurement buyer draft a request for quotation (RFx).

Your job is not to write prose. It is to make every reply comparable later. That means:
- every line has a unit prices will be compared in (canonical_uom), an annual quantity, and a
  specification with the attributes that define the item;
- if a line can be quoted in another unit (packs, weight, length), the conversion factor goes
  in conversion_factors, e.g. {"name": "pieces_per_pack", "value": "12"};
- every pass/fail (gating) question has an explicit pass rule;
- the terms state price_basis, currency, payment_days and delivery, and a reply-by date
  (due).

Work with the tools. The draft shown with each message is the current truth; the buyer may
have edited it directly, so never undo their edits. Only change what the buyer asked for or
what is clearly needed. When the buyer refers to last year or an earlier event, use
import_from_history. After changing the draft, call check_completeness; if anything is missing
that only the buyer can know (a quantity, a size, a pass rule), ask one short, specific
question rather than guessing. Never invent quantities, prices or specifications.

"Last year" or "the previous event" means the most recent awarded event: import_from_history
picks it and names it in its result; call it by the number and title the result gives.

How you write to the buyer:
- Plain words only. Never show tool names, field names (canonical_uom, pass_rule,
  payment_days...), rule operators (<=, valid_on, is_yes) or JSON. Say "10 or less", "must be
  yes", "valid on the award date", "unit prices are compared in".
- Dates as 14 Mar 2025. Quantities and rupees with Indian grouping (80,000; ₹1,23,45,678),
  copied from the draft, never computed.
- Ask about a missing point once. If you asked in an earlier turn and the buyer did not
  answer it, do not ask again unless they bring it up; it stays in the checklist on the page.

You cannot send the RFx. When the draft is complete, tell the buyer to review the email
preview and press Send. Keep replies short: what you changed, then any question."""


def _nullable(t: str | dict) -> dict:
    return {"anyOf": [t if isinstance(t, dict) else {"type": t}, {"type": "null"}]}


def _obj(props: dict) -> dict:
    """A strict object schema: every property listed and required, nothing else allowed."""
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


PAIRS = {"type": "array", "items": _obj({"name": {"type": "string"}, "value": {"type": "string"}})}

TOOLS = [
    {
        "name": "import_from_history",
        "description": "Start this draft from the most recent awarded event: copies its lines "
        "(spec, unit, quantity, plant, conversion factors), questionnaire, terms and invited "
        "vendors. What the draft already has is kept. The result names the event copied; "
        "tell the buyer which one it was.",
        "input_schema": _obj({}),
    },
    {
        "name": "upsert_lines",
        "description": "Add or change lines by line_no. Fields set to null are left unchanged. "
        "line_no null adds a new line at the end. spec and conversion_factors "
        "replace the whole set for that line when given.",
        "input_schema": _obj(
            {
                "lines": {
                    "type": "array",
                    "items": _obj(
                        {
                            "line_no": _nullable("integer"),
                            "description": _nullable("string"),
                            "canonical_uom": _nullable("string"),
                            "annual_qty": _nullable("string"),
                            "plant": _nullable("string"),
                            "spec": _nullable(PAIRS),
                            "conversion_factors": _nullable(PAIRS),
                        }
                    ),
                }
            }
        ),
    },
    {
        "name": "remove_lines",
        "description": "Remove lines by line number.",
        "input_schema": _obj({"line_nos": {"type": "array", "items": {"type": "integer"}}}),
    },
    {
        "name": "set_terms",
        "description": "Set commercial terms by key: price_basis, currency, payment_days, "
        "delivery, plants, validity_days, due (the reply-by date, YYYY-MM-DD) and award_date "
        "(YYYY-MM-DD). A null value removes the term.",
        "input_schema": _obj(
            {
                "terms": {
                    "type": "array",
                    "items": _obj({"key": {"type": "string"}, "value": _nullable("string")}),
                }
            }
        ),
    },
    {
        "name": "upsert_questions",
        "description": "Add or change questionnaire questions by q_no (null adds at the end). "
        "Gating questions need a pass_rule: op is one of <=, <, >=, >, == (numeric "
        "answers, compared with value), is_yes (answer must be yes), valid_on (a "
        "certificate valid on the date in value, YYYY-MM-DD), or valid_on_date (valid on the "
        "award date; value null). These are for the tool only: describe rules to the buyer "
        "in words.",
        "input_schema": _obj(
            {
                "questions": {
                    "type": "array",
                    "items": _obj(
                        {
                            "q_no": _nullable("integer"),
                            "section": {"type": "string"},
                            "text": {"type": "string"},
                            "answer_type": {
                                "type": "string",
                                "enum": ["yes_no", "number", "date", "text", "file"],
                            },
                            "is_gating": {"type": "boolean"},
                            "pass_rule": _nullable(
                                _obj(
                                    {
                                        "op": {"type": "string", "enum": list(draft.PASS_OPS)},
                                        "value": _nullable("string"),
                                    }
                                )
                            ),
                        }
                    ),
                }
            }
        ),
    },
    {
        "name": "check_completeness",
        "description": "List what is still missing for replies to be comparable. Empty = complete.",
        "input_schema": _obj({}),
    },
    {
        "name": "preview_email",
        "description": "Show the request email that Send would deliver (sending is the buyer's).",
        "input_schema": _obj({}),
    },
]
for _tool in TOOLS:
    _tool["strict"] = True  # inputs always match the schema


def _pairs(items: list[dict] | None) -> dict | None:
    return None if items is None else {p["name"]: p["value"] for p in items}


def run_tool(rfx_id: int, name: str, args: dict) -> object:
    """Execute one tool against the draft. Returns something JSON-serialisable."""
    actor = "copilot"
    if name == "import_from_history":
        return draft.import_from_history(rfx_id, actor=actor)
    if name == "upsert_lines":
        lines = [
            {
                **line,
                "spec": _pairs(line.get("spec")),
                "conversion_factors": _pairs(line.get("conversion_factors")),
            }
            for line in args["lines"]
        ]
        return {"changed_line_nos": draft.upsert_lines(rfx_id, lines, actor=actor)}
    if name == "remove_lines":
        return {"removed": draft.remove_lines(rfx_id, args["line_nos"], actor=actor)}
    if name == "set_terms":
        return draft.set_terms(rfx_id, {t["key"]: t["value"] for t in args["terms"]}, actor=actor)
    if name == "upsert_questions":
        return {"q_nos": draft.upsert_questions(rfx_id, args["questions"], actor=actor)}
    if name == "check_completeness":
        return {"issues": draft.check_completeness(rfx_id)}
    if name == "preview_email":
        subject, body = draft.render_email(rfx_id)
        return {"subject": subject, "body": body}
    raise ValueError(f"unknown tool {name}")


# --- Thread persistence ------------------------------------------------------------------


def thread_for(rfx_id: int) -> int:
    with db.begin() as conn:
        tid = conn.execute(
            sa.select(db.thread.c.id).where(
                db.thread.c.kind == "copilot", db.thread.c.rfx_id == rfx_id
            )
        ).scalar()
        if tid is None:
            tid = conn.execute(
                db.thread.insert().values(kind="copilot", rfx_id=rfx_id, title="RFx co-pilot")
            ).inserted_primary_key[0]
    return tid


def history(thread_id: int) -> list[dict]:
    """The visible chat: [{"role", "text"}], oldest first."""
    with db.connect() as conn:
        rows = (
            conn.execute(
                sa.select(db.chat_message)
                .where(db.chat_message.c.thread_id == thread_id)
                .order_by(db.chat_message.c.id)
            )
            .mappings()
            .all()
        )
    return [{"role": r["role"], "text": (r["content"] or {}).get("text", "")} for r in rows]


def _save(thread_id: int, role: str, text: str) -> int:
    with db.begin() as conn:
        return conn.execute(
            db.chat_message.insert().values(thread_id=thread_id, role=role, content={"text": text})
        ).inserted_primary_key[0]


def _save_tool(thread_id: int, name: str, args: dict, output, error: str | None, ms: int) -> None:
    with db.begin() as conn:
        conn.execute(
            db.tool_call.insert().values(
                thread_id=thread_id,
                name=name,
                input=args,
                output=json.loads(draft.as_json(output)) if output is not None else None,
                error=error,
                latency_ms=ms,
            )
        )


# --- One turn ----------------------------------------------------------------------------


def run_turn(
    rfx_id: int,
    user_text: str,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[str, dict], None] | None = None,
) -> str:
    """Handle one buyer message end to end and return the co-pilot's reply text."""
    thread_id = thread_for(rfx_id)
    past = history(thread_id)  # earlier turns as plain text; tool details live in tool_call
    _save(thread_id, "user", user_text)
    state = (
        f"Current draft (JSON):\n{draft.as_json(draft.snapshot(rfx_id))}\n\n"
        f"Completeness issues found by code: {draft.as_json(draft.check_completeness(rfx_id))}"
    )
    messages = [{"role": m["role"], "content": m["text"]} for m in past if m["text"]]
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": state},
                {"type": "text", "text": f"Buyer: {user_text}"},
            ],
        }
    )

    parts: list[str] = []  # text the model wrote across tool rounds
    for _ in range(MAX_STEPS):
        result = llm.complete(
            messages, system=SYSTEM, tools=TOOLS, effort="medium", max_tokens=16000, on_text=on_text
        )
        msg = result.message
        messages.append({"role": "assistant", "content": msg.content})
        if result.text.strip():
            parts.append(result.text.strip())
        uses = [b for b in msg.content if b.type == "tool_use"]
        if msg.stop_reason != "tool_use" or not uses:
            break
        results = []
        for block in uses:  # all results go back in one user message
            if on_tool:
                on_tool(block.name, block.input)
            started = time.monotonic()
            try:
                output, error = run_tool(rfx_id, block.name, block.input), None
            except (ValueError, KeyError) as exc:
                output, error = None, str(exc)
            _save_tool(
                thread_id,
                block.name,
                block.input,
                output,
                error,
                int((time.monotonic() - started) * 1000),
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": draft.as_json(output) if error is None else f"Error: {error}",
                    "is_error": error is not None,
                }
            )
        messages.append({"role": "user", "content": results})
        if parts and on_text:
            on_text("\n\n")  # keep text from separate rounds apart while streaming
    else:
        parts.append("(Stopped after too many steps; please check the draft.)")
    reply = "\n\n".join(parts) or "Done. The draft on the right is updated."
    _save(thread_id, "assistant", reply)
    return reply
