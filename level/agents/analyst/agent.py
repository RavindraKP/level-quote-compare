"""The analyst agent: plain-language questions over the comparison, answered with tools.

One turn:
1. Earlier turns of the thread are replayed as plain text; the new question is added.
2. The model calls tools (tools.py) until it answers in text, at most MAX_STEPS rounds.
3. T9: every number in the answer must appear in a tool result of this thread (this turn or
   an earlier one, whose figures may be repeated) (grounding.py).
   If not, the model is told which numbers and tries once more; if it still fails, the
   answer is kept but marked with a visible warning.
4. T10: the caveats of every scenario run this turn are returned separately, so the page
   shows them whether or not the model mentioned them.
5. The question, answer, artefacts and every tool call are saved in SQLite.

Runs from a plain Python call (evals use `ask`); no Streamlit anywhere.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlalchemy as sa

from level import db, llm, worker
from level.agents.analyst import grounding, prompt, tools
from level.types import Job, JobStatus

JOB_KIND = "analyst.ask"

MAX_STEPS = 10
GROUNDING_RETRY = (
    "Automatic check (from the system, not the buyer; the buyer does not see this): these "
    "numbers in your draft answer do not appear in any tool result: {numbers}. Write the "
    "answer again using only numbers copied from tool results. If you need a figure no tool "
    "has given you (a sum, difference or average), call a tool that computes it; do not work "
    "it out yourself. Do not mention this check, and do not comment on earlier replies.")


@dataclass
class Answer:
    text: str
    thread_id: int
    message_id: int | None = None
    artefacts: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    scenario_ids: list[int] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)  # tool calls: name, input, output/error
    ungrounded: list[str] = field(default_factory=list)  # numbers that failed T9 twice
    retried: bool = False

    def __str__(self) -> str:
        return self.text


# --- Threads -----------------------------------------------------------------------------


def new_thread(rfx_id: int, title: str = "Analyst") -> int:
    with db.begin() as conn:
        return conn.execute(db.thread.insert().values(kind="analyst", rfx_id=rfx_id,
                                                      title=title)).inserted_primary_key[0]


def latest_thread(rfx_id: int) -> int:
    """The newest analyst thread for this RFx, created if there is none."""
    with db.connect() as conn:
        tid = conn.execute(sa.select(sa.func.max(db.thread.c.id)).where(
            db.thread.c.kind == "analyst", db.thread.c.rfx_id == rfx_id)).scalar()
    return tid or new_thread(rfx_id)


def messages(thread_id: int) -> list[dict]:
    """Saved chat: [{"id", "role", "content"}] oldest first; content holds text, artefacts..."""
    with db.connect() as conn:
        rows = conn.execute(sa.select(db.chat_message).where(
            db.chat_message.c.thread_id == thread_id).order_by(db.chat_message.c.id)
        ).mappings().all()
    return [{"id": r["id"], "role": r["role"], "content": r["content"] or {}} for r in rows]


def tool_calls(message_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(sa.select(db.tool_call).where(
            db.tool_call.c.message_id == message_id).order_by(db.tool_call.c.id)
        ).mappings().all()
    return [dict(r) for r in rows]


def _earlier_outputs(thread_id: int) -> list:
    """Tool results from earlier turns of this thread. Their figures were grounded when
    first used, and follow-up answers may repeat them (earlier turns are replayed as text)."""
    with db.connect() as conn:
        return list(conn.execute(sa.select(db.tool_call.c.output).where(
            db.tool_call.c.thread_id == thread_id, db.tool_call.c.error.is_(None))).scalars())


def _vendor_terms(rfx_id: int) -> list[str]:
    """The vendors' own terms on this event, as text. A figure an answer repeats from them
    (a rebate threshold, a validity date) is the vendor's, not one the model made up."""
    with db.connect() as conn:
        rows = conn.execute(sa.select(db.vendor_term.c.text, db.vendor_term.c.value,
                                      db.vendor_term.c.conditions).where(
            db.vendor_term.c.rfx_id == rfx_id)).all()
    return [" ".join(str(x) for x in r if x) for r in rows]


def _save_message(thread_id: int, role: str, content: dict) -> int:
    with db.begin() as conn:
        return conn.execute(db.chat_message.insert().values(
            thread_id=thread_id, role=role, content=tools.jsonable(content))
        ).inserted_primary_key[0]


def _save_trace(thread_id: int, message_id: int, trace: list[dict]) -> None:
    if not trace:
        return
    with db.begin() as conn:
        conn.execute(db.tool_call.insert(), [{
            "thread_id": thread_id, "message_id": message_id, "name": t["name"],
            "input": tools.jsonable(t["input"]), "output": tools.jsonable(t.get("output")),
            "error": t.get("error"), "latency_ms": t["latency_ms"]} for t in trace])


# --- One turn ----------------------------------------------------------------------------


def _call_tool(ctx: tools.TurnContext, block) -> tuple[dict, str, bool]:
    """Run one tool_use block -> (trace entry, result text, is_error)."""
    started = time.monotonic()
    try:
        output = tools.run(ctx, block.name, block.input)
        entry = {"name": block.name, "input": block.input, "output": output}
        text, is_error = tools.to_text(output), False
    except (tools.ToolError, KeyError, ValueError, ArithmeticError) as exc:
        entry = {"name": block.name, "input": block.input, "error": str(exc)}
        text, is_error = f"Error: {exc}", True
    entry["latency_ms"] = int((time.monotonic() - started) * 1000)
    return entry, text, is_error


def ask(rfx_id: int, question: str, thread_id: int | None = None,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict], None] | None = None,
        question_id: int | None = None) -> Answer:
    """Answer one question. Without a thread_id a fresh thread is used (as the evals do).
    question_id: the question is already saved as that message (the page saves it first,
    then the worker answers it), so only earlier messages are replayed."""
    thread_id = thread_id or new_thread(rfx_id, title=question[:60])
    past = [m for m in messages(thread_id) if m["content"].get("text")
            and (question_id is None or m["id"] < question_id)]
    convo = [{"role": m["role"], "content": m["content"]["text"]} for m in past]
    convo.append({"role": "user", "content": question})
    if question_id is None:
        _save_message(thread_id, "user", {"text": question})

    system = prompt.system_prompt(rfx_id)
    ctx = tools.TurnContext(rfx_id=rfx_id)
    trace: list[dict] = []
    outputs: list = _earlier_outputs(thread_id)  # every tool result, for the T9 check
    outputs.append(_vendor_terms(rfx_id))  # the vendors' own figures count too (H9)
    text, retried, ungrounded = "", False, []
    for _ in range(MAX_STEPS):
        result = llm.complete(convo, system=system, tools=prompt.TOOLS, effort="medium",
                              max_tokens=16000, on_text=on_text)
        msg = result.message
        convo.append({"role": "assistant", "content": msg.content})
        uses = [b for b in msg.content if b.type == "tool_use"]
        if msg.stop_reason == "tool_use" and uses:
            replies = []
            for block in uses:  # all results go back in one user message
                if on_tool:
                    on_tool(block.name, block.input)
                entry, out_text, is_error = _call_tool(ctx, block)
                trace.append(entry)
                if not is_error:
                    outputs.append(out_text)
                replies.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out_text, "is_error": is_error})
            convo.append({"role": "user", "content": replies})
            continue
        text = result.text.strip()
        ungrounded = grounding.unmatched(text, outputs, question)
        if ungrounded and not retried:  # T9: one automatic retry, naming the numbers
            retried = True
            convo.append({"role": "user", "content": GROUNDING_RETRY.format(
                numbers=", ".join(ungrounded))})
            if on_text:
                on_text("\n\n*(checking the figures again)*\n\n")
            continue
        break
    else:
        text = text or "I stopped after too many steps without an answer. Please rephrase."

    answer = Answer(text=text, thread_id=thread_id, artefacts=ctx.artefacts,
                    caveats=ctx.caveats, scenario_ids=ctx.scenario_ids, trace=trace,
                    ungrounded=ungrounded, retried=retried)
    answer.message_id = _save_message(thread_id, "assistant", {
        "text": text, "artefacts": answer.artefacts, "caveats": answer.caveats,
        "scenario_ids": answer.scenario_ids, "ungrounded": ungrounded, "retried": retried})
    _save_trace(thread_id, answer.message_id, trace)
    return answer


# --- Answering in the background (UX audit H5) -------------------------------------------
# The page saves the question and queues a job; the worker answers it. The answer is then
# saved even if the buyer leaves the page or closes the tab, and a second question cannot
# cut the first one off.


def save_question(thread_id: int, question: str) -> int:
    """Store the buyer's question at once, so it shows while the answer is worked out."""
    return _save_message(thread_id, "user", {"text": question})


def queue_answer(rfx_id: int, thread_id: int, question_id: int) -> int:
    """Queue the worker to answer a saved question. Returns the job id."""
    target = json.dumps({"rfx_id": rfx_id, "thread_id": thread_id, "question_id": question_id})
    return worker.enqueue(JOB_KIND, target)


def answered(thread_id: int, question_id: int) -> bool:
    """Has the question got an answer (an assistant message after it)?"""
    with db.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).where(
            db.chat_message.c.thread_id == thread_id, db.chat_message.c.role == "assistant",
            db.chat_message.c.id > question_id)).scalar() > 0


def jobs_for_thread(thread_id: int) -> dict[int, Job]:
    """Question message id -> its newest answering job, for this thread."""
    with db.connect() as conn:
        rows = conn.execute(sa.select(db.job).where(db.job.c.kind == JOB_KIND).order_by(
            db.job.c.id)).mappings().all()
    out = {}
    for r in rows:
        target = json.loads(r["target"])
        if target["thread_id"] == thread_id:
            out[target["question_id"]] = Job.model_validate(dict(r))
    return out


def busy(thread_id: int) -> Job | None:
    """The job answering a question in this thread right now, if any."""
    return next((j for j in jobs_for_thread(thread_id).values()
                 if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)), None)


@worker.handler(JOB_KIND)
def _answer_job(ctx: worker.JobContext) -> None:
    """Worker: answer one saved question. Idempotent: an answered question is left alone."""
    target = json.loads(ctx.target)
    thread_id, question_id = target["thread_id"], target["question_id"]
    if answered(thread_id, question_id):
        return
    with db.connect() as conn:
        content = conn.execute(sa.select(db.chat_message.c.content).where(
            db.chat_message.c.id == question_id)).scalar() or {}
    ctx.progress("Reading the question", 0.05)

    def on_tool(name: str, _args: dict) -> None:
        ctx.progress(name, 0.5)  # the page names the step in words

    ask(target["rfx_id"], content.get("text", ""), thread_id=thread_id, on_tool=on_tool,
        question_id=question_id)
