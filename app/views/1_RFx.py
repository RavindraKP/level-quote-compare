"""RFx co-pilot (WP5): chat on the left, the live draft on the right. Layout and calls only.

Both sides write to SQLite through level/agents/copilot/draft.py and the page re-reads the
draft on every render, so chat edits and direct edits never overwrite each other.
"""

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st
from components import data, frame

from level import copy, llm
from level.agents.copilot import agent, draft

log = logging.getLogger(__name__)


def _dec(value) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _qty(value) -> str:
    """'80000' -> '80,000' (Indian grouping); anything unreadable as it is."""
    try:
        return copy.number(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return "" if value is None else str(value)


def _slug(label: str) -> str:
    """A name typed by the buyer as a stored attribute name: 'Board grade' -> 'board_grade'."""
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _day(text) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


AVATAR = {"user": ":material/person:", "assistant": ":material/smart_toy:"}
TYPE_KEY = {label: key for key, label in copy.ANSWER_TYPE_LABEL.items()}


def past_events_panel(rid: int | None, expanded: bool) -> None:
    """Past events: pick one to read it; "Start from this event" copies it into the draft."""
    events = draft.past_events(exclude_rfx_id=rid)
    with st.expander(copy.PAST_EVENTS, expanded=expanded):
        if not events:
            st.info(copy.PAST_EVENTS_EMPTY)
            return
        table = pd.DataFrame([{
            "number": e["number"], "title": e["title"], "year": e["year"],
            "status": copy.EVENT_STATUS.get(e["status"], e["status"].capitalize()),
            "invited": e["invited"],
            "suppliers": ", ".join(e["suppliers"]) or copy.NOT_AWARDED,
            "award_value": copy.inr_short(e["award_value"]),
            "award_date": copy.date(e["award_date"]) if e["award_date"] else "—",
        } for e in events]).rename(columns=copy.PAST_COLUMNS)
        cols = copy.PAST_COLUMNS
        picked = st.dataframe(
            table, hide_index=True, width="stretch", on_select="rerun",
            selection_mode="single-row", key=f"past_{rid}",
            column_config={cols["title"]: st.column_config.TextColumn(width="medium"),
                           cols["number"]: st.column_config.TextColumn(width=140),
                           cols["year"]: st.column_config.TextColumn(width=60),
                           cols["invited"]: st.column_config.NumberColumn(width=70),
                           cols["suppliers"]: st.column_config.TextColumn(width="medium")})
        st.caption(copy.AWARD_VALUE_BASIS)
        if not picked.selection.rows:
            st.caption(copy.PAST_EVENTS_PICK)
            return
        event = events[picked.selection.rows[0]]
        detail = draft.past_event(event["id"])
        head, button = st.columns([3, 1], vertical_alignment="center")
        head.markdown(f"**{event['number']} · {event['title']}**")
        if event["award_value"] is not None:
            head.caption(f"{copy.PAST_COLUMNS['award_value']}: {copy.inr(event['award_value'])}")
        if button.button(copy.START_FROM, type="primary", help=copy.START_FROM_HELP,
                         key=f"start_{event['id']}"):
            target = rid or draft.new_draft()
            result = draft.import_from_history(target, event["id"])
            st.session_state["started_from"] = copy.started_from(result)
            st.rerun()
        st.caption(copy.PAST_READ_ONLY)
        p_lines, p_q, p_terms = st.tabs(["Lines", "Questionnaire", "Terms"])
        p_lines.dataframe(pd.DataFrame([{
            **line, "spec": copy.spec_text(line["spec"]),
            "annual_qty": copy.number(_dec(line["annual_qty"])),
            "awarded_to": line["awarded_to"] or copy.NOT_AWARDED,
            "awarded_price": copy.unit_price(_dec(line["awarded_price"])),
        } for line in detail["lines"]], columns=list(copy.PAST_LINE_COLUMNS))
            .rename(columns=copy.PAST_LINE_COLUMNS), hide_index=True, width="stretch")
        p_q.dataframe(pd.DataFrame([{
            "Q": q["q_no"], "Section": q["section"], "Question": q["text"],
            "Answer": copy.ANSWER_TYPE_LABEL.get(q["answer_type"], q["answer_type"]),
            "Required": "Yes" if q["is_gating"] else "No",
            "Pass rule": draft.format_rule(q["pass_rule"]),
        } for q in detail["questions"]]), hide_index=True, width="stretch")
        p_terms.dataframe(pd.DataFrame(copy.term_rows(detail["terms"])), hide_index=True,
                          width="stretch")


rfx = draft.current_draft()
frame.header(data.current_rfx(rfx["id"] if rfx else st.session_state.get("rfx_id")), "RFx")
if rfx is None:
    shown = data.current_rfx(st.session_state.get("rfx_id"))
    if just_sent := st.session_state.get("just_sent"):
        # B10: sending never moves the pages to the new event; the buyer chooses to.
        st.success(copy.SENT_NOTE.format(current=copy.event_number(shown) if shown
                                         else "the event above"))
        if shown is None or shown["id"] != just_sent:
            if st.button("Open the new event's Inbox", type="primary"):
                st.session_state["rfx_id"] = just_sent
                st.session_state.pop("just_sent")
                st.switch_page("views/2_Inbox.py")
    sent = shown is not None
    st.info(("No RFx draft is open; the event above has already been sent to vendors. "
             if sent else "No RFx yet. ")
            + "Start a new RFx, then describe what you need in the chat: the co-pilot fills "
            "in lines, terms and the questionnaire for you to check.")
    if st.button("Start a new RFx", type="primary"):
        draft.new_draft()
        st.rerun()
    past_events_panel(None, expanded=True)
    st.stop()
rid = rfx["id"]
ver = draft.version(rid)  # editor keys include this, so they reload after any change

TOOL_LABEL = {"pieces_per_pack": "Worked out a pack size",
              "import_from_history": "Started from an earlier event",
              "upsert_lines": "Updated lines", "remove_lines": "Removed lines",
              "set_terms": "Updated terms", "upsert_questions": "Updated the questionnaire",
              "check_completeness": "Checked the draft", "preview_email": "Wrote the email"}

chat_col, draft_col = st.columns([2, 3], gap="large")

# --- Chat --------------------------------------------------------------------------------
with chat_col:
    st.subheader("Co-pilot")
    box = st.container(height=560)
    with box:
        history = agent.history(agent.thread_for(rid))
        if not history:
            st.caption("Tell the co-pilot what you need, for example \"Same lines as last year, "
                       "60-day payment, delivered to our plants\". It fills in the draft on the "
                       "right and asks about anything only you can decide.")
        for m in history:
            text = m["text"] if m["role"] == "user" else copy.plain(m["text"])  # display only
            st.chat_message(m["role"], avatar=AVATAR.get(m["role"])).markdown(text)
    # Disabled while a request runs, so a second message cannot cut the first short (H5).
    prompt = st.chat_input("e.g. Same lines as last year, 60-day payment, delivered",
                           submit_mode="disable")
    if prompt:
        with box:
            st.chat_message("user", avatar=AVATAR["user"]).markdown(prompt)
            with st.chat_message("assistant", avatar=AVATAR["assistant"]):
                steps = st.empty()
                steps.caption("Reading the draft…")  # shown at once, before the first word
                out = st.empty()
                streamed: list[str] = []
                used: list[str] = []

                def show_text(delta: str) -> None:
                    streamed.append(delta)
                    out.markdown(copy.plain("".join(streamed)))

                def show_tool(name: str, _args: dict) -> None:
                    used.append(TOOL_LABEL.get(name, "Working"))
                    steps.caption(" → ".join(used))

                try:
                    agent.run_turn(rid, prompt, on_text=show_text, on_tool=show_tool)
                except llm.MissingAPIKey:
                    st.error("The co-pilot is not available: this app has no API key set. "
                             "You can still edit the draft directly on the right.")
                    st.stop()
                except llm.LLMError:
                    st.error("The co-pilot could not finish that request. Ask again, or edit "
                             "the draft directly on the right.")
                    st.stop()
        st.rerun()  # redraw the draft with the co-pilot's changes

# --- Draft -------------------------------------------------------------------------------


@st.dialog("Specification and conversions", width="large")
def spec_dialog(line: dict) -> None:
    """Edit one line's attributes and conversion factors as labelled rows (B7)."""
    st.markdown(f"**Line {line['line_no']} · {line['description'] or ''}**")
    spec_rows = pd.DataFrame(
        [{"Attribute": copy.spec_label(k), "Value": str(v), "key": k}
         for k, v in (line["spec"] or {}).items()], columns=["Attribute", "Value", "key"])
    spec = st.data_editor(spec_rows, num_rows="dynamic", hide_index=True, width="stretch",
                          column_order=["Attribute", "Value"], key=f"spec_{line['line_no']}")
    st.caption("What defines the item, for example size, grade or material.")
    conv_rows = []
    for k, v in (line["conversion_factors"] or {}).items():
        unit, _, per = k.partition("_per_")
        conv_rows.append({"Amount": str(v), "Unit": unit.replace("_", " "),
                          "Per": per.replace("_", " "), "key": k})
    conv = st.data_editor(pd.DataFrame(conv_rows, columns=["Amount", "Unit", "Per", "key"]),
                          num_rows="dynamic", hide_index=True, width="stretch",
                          column_order=["Amount", "Unit", "Per"],
                          key=f"conv_{line['line_no']}")
    st.caption("How to convert other units, for example 454 g per piece or 12 pieces per pack. "
               "This is what lets a per-kg or per-pack quote be compared without guessing.")
    if st.button(f"Save line {line['line_no']}", type="primary"):
        new_spec, new_conv = {}, {}
        for r in spec.to_dict("records"):
            if not str(r.get("Attribute") or "").strip():
                continue
            same = r.get("key") and copy.spec_label(r["key"]) == r["Attribute"]
            new_spec[r["key"] if same else _slug(r["Attribute"])] = str(r.get("Value") or "")
        for r in conv.to_dict("records"):
            if not str(r.get("Amount") or "").strip() or not str(r.get("Unit") or "").strip():
                continue
            per = _slug(str(r.get("Per") or ""))
            new_conv[f"{_slug(r['Unit'])}_per_{per}" if per else _slug(r["Unit"])] = \
                str(r["Amount"]).strip()
        draft.upsert_lines(rid, [{"line_no": line["line_no"], "spec": new_spec,
                                  "conversion_factors": new_conv}])
        st.rerun()


def terms_form(terms: dict, plants_in_lines: list[str]) -> None:
    """The terms as labelled fields (B7); anything else the draft holds is listed below."""
    known = ("price_basis", "currency", "payment_days", "delivery", "plants", "due",
             "award_date", "validity_days")
    with st.form(f"terms_{rid}_{ver}", border=False):
        c1, c2 = st.columns(2)
        basis = c1.text_input(copy.field_label("price_basis"), terms.get("price_basis", ""),
                              placeholder="e.g. per piece, ex-GST, delivered to plant")
        currency = c2.text_input(copy.field_label("currency"), terms.get("currency", ""),
                                 placeholder="e.g. INR")
        pay = c1.text_input(copy.field_label("payment_days"), terms.get("payment_days", ""),
                            placeholder="e.g. 60")
        valid = c2.text_input(copy.field_label("validity_days"),
                              terms.get("validity_days", ""), placeholder="e.g. 90")
        delivery = st.text_input(copy.field_label("delivery"), terms.get("delivery", ""),
                                 placeholder="e.g. delivered to each plant")
        have = terms.get("plants") or []
        have = have if isinstance(have, list) else [p.strip() for p in str(have).split(",")]
        plants = st.multiselect(copy.field_label("plants"),
                                sorted(set(plants_in_lines) | set(have)), default=have,
                                accept_new_options=True,
                                help="Pick the plants on your lines, or type a new one.")
        d1, d2 = st.columns(2)
        due = d1.date_input(copy.field_label("due"), _day(terms.get("due")), format="DD/MM/YYYY",
                            help="Vendors are asked to reply by this date.")
        award = d2.date_input(copy.field_label("award_date"), _day(terms.get("award_date")),
                              format="DD/MM/YYYY",
                              help="Certificates are checked as valid on this date.")
        others = [{"Term": copy.field_label(k), "Value": copy.term_value(v), "key": k}
                  for k, v in terms.items() if k not in known and k not in draft.EVENT_KEYS
                  and k not in ("category", "buyer")]
        st.markdown("**Other terms**")
        other = st.data_editor(pd.DataFrame(others, columns=["Term", "Value", "key"]),
                               num_rows="dynamic", hide_index=True, width="stretch",
                               column_order=["Term", "Value"], key=f"other_terms_{rid}_{ver}")
        if st.form_submit_button("Save terms", type="primary"):
            new = {"price_basis": basis.strip() or None, "currency": currency.strip() or None,
                   "payment_days": pay.strip() or None, "validity_days": valid.strip() or None,
                   "delivery": delivery.strip() or None, "plants": plants or None,
                   "due": _iso(due), "award_date": _iso(award)}
            kept = {}
            for r in other.to_dict("records"):
                if str(r.get("Term") or "").strip():
                    same = r.get("key") and copy.field_label(r["key"]) == r["Term"]
                    kept[r["key"] if same else _slug(r["Term"])] = str(r.get("Value") or "")
            gone = {o["key"]: None for o in others if o["key"] not in kept}
            # older names of the same terms are replaced by the names used now
            legacy = {k: None for k in draft.TERM_ALIASES if k in snap["terms"]}
            draft.set_terms(rid, legacy | gone | kept | new)
            st.rerun()


with draft_col:
    snap = draft.snapshot(rid)
    if note := st.session_state.pop("started_from", None):
        st.success(note)
    past_events_panel(rid, expanded=not snap["lines"])
    with st.form("header", border=False):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 1], vertical_alignment="bottom")
        title = c1.text_input("Title", snap["title"])
        category = c2.text_input("Category", snap["category"] or "")
        ref = c3.text_input("Event no.", snap["terms"].get("ref") or "",
                            help="Your own reference for this event, shown to vendors.")
        if c4.form_submit_button("Save"):
            draft.set_header(rid, title, category or None)
            if ref.strip() != (snap["terms"].get("ref") or ""):
                draft.set_terms(rid, {"ref": ref.strip() or None})
            st.rerun()

    issues = draft.check_completeness(rid)
    if issues:
        with st.expander(f"{copy.plural(len(issues), 'point')} to settle before sending",
                         expanded=True):
            st.markdown("\n".join(f"- {i}" for i in issues))
    else:
        st.success(
            "Complete: every line has a unit, quantity and specification; every required "
            "question has a pass rule; the terms and reply date are set."
        )

    t_lines, t_q, t_terms, t_send = st.tabs(["Lines", "Questionnaire", "Terms", "Vendors & send"])

    with t_lines:
        ref_prices = draft.reference_prices(rid)  # buyer only; never in snapshot() or sent
        rows = [
            {
                **line,
                "spec": copy.spec_text(line["spec"]),
                "conversion_factors": copy.conversion_text(line["conversion_factors"]),
                "annual_qty": _qty(line["annual_qty"]),
                "last_price": copy.unit_price(_dec(ref_prices.get(line["line_no"], {})
                                                   .get("price"))),
                "last_supplier": ref_prices.get(line["line_no"], {}).get("supplier") or "—",
            }
            for line in snap["lines"]
        ]
        cols = ["line_no", "description", "spec", "canonical_uom", "annual_qty", "last_price",
                "last_supplier", "plant", "conversion_factors"]
        key = f"lines_{rid}_{ver}"

        def save_lines() -> None:
            delta = st.session_state[key]
            try:
                draft.apply_line_edits(
                    rid, rows, delta["edited_rows"], delta["added_rows"], delta["deleted_rows"]
                )
            except ValueError as exc:
                st.session_state["edit_error"] = str(exc)

        st.data_editor(
            pd.DataFrame(rows, columns=cols),
            key=key,
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            on_change=save_lines,
            column_config={
                "line_no": st.column_config.NumberColumn("No.", width=50),
                "description": st.column_config.TextColumn("Description", width="medium"),
                "spec": st.column_config.TextColumn("Specification", width="large",
                                                    disabled=True),
                "canonical_uom": st.column_config.TextColumn("Unit", width=70),
                "annual_qty": st.column_config.TextColumn("Annual qty", width=90,
                                                          alignment="right"),
                "plant": st.column_config.TextColumn("Plant", width=90),
                "conversion_factors": st.column_config.TextColumn("Conversions",
                                                                  disabled=True),
                "last_price": st.column_config.TextColumn(copy.REF_PRICE, disabled=True),
                "last_supplier": st.column_config.TextColumn(copy.REF_SUPPLIER, disabled=True),
            },
        )
        st.caption("Edits save as you make them. Unit = the unit prices are compared in. "
                   + copy.REF_CAPTION)
        if snap["lines"]:
            pick_col, edit_col = st.columns([3, 1], vertical_alignment="bottom")
            chosen = pick_col.selectbox(
                "Specification and conversions of line", snap["lines"],
                format_func=lambda ln: f"Line {ln['line_no']}: {ln['description'] or ''}",
                key=f"spec_pick_{rid}")
            if edit_col.button("Edit", width="stretch"):
                spec_dialog(chosen)

    with t_q:
        qrows = [{**q, "pass_rule": draft.format_rule(q["pass_rule"]),
                  "answer_type": copy.ANSWER_TYPE_LABEL.get(q["answer_type"], q["answer_type"])}
                 for q in snap["questions"]]
        qkey = f"questions_{rid}_{ver}"

        def _type_key(change: dict) -> dict:
            if "answer_type" in change:  # the label shown -> the stored answer type
                change = {**change, "answer_type": TYPE_KEY.get(change["answer_type"],
                                                                change["answer_type"])}
            return change

        def save_questions() -> None:
            delta = st.session_state[qkey]
            try:
                draft.apply_question_edits(
                    rid, qrows, {i: _type_key(c) for i, c in delta["edited_rows"].items()},
                    [_type_key(a) for a in delta["added_rows"]], delta["deleted_rows"]
                )
            except ValueError as exc:
                st.session_state["edit_error"] = str(exc)

        st.data_editor(
            pd.DataFrame(
                qrows, columns=["q_no", "section", "text", "answer_type", "is_gating", "pass_rule"]
            ),
            key=qkey,
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            on_change=save_questions,
            column_config={
                "q_no": st.column_config.NumberColumn("Q", width=40),
                "section": st.column_config.TextColumn("Section", width=100),
                "text": st.column_config.TextColumn("Question", width="medium"),
                "answer_type": st.column_config.SelectboxColumn(
                    "Answer", options=list(copy.ANSWER_TYPE_LABEL.values()), width=90
                ),
                "is_gating": st.column_config.CheckboxColumn("Required", width=70),
                "pass_rule": st.column_config.TextColumn("Pass rule", width=170),
            },
        )
        st.caption("Write a pass rule in words: \"10 or less\", \"5 or more\", \"Must be yes\", "
                   "\"Valid on the award date\" or \"Valid on 14 Mar 2025\".")

    with t_terms:
        terms_form(draft.standard_terms(snap["terms"]),
                   sorted({ln["plant"] for ln in snap["lines"] if ln["plant"]}))

    with t_send:
        vendors = draft.all_vendors()
        names = {v["id"]: v["name"] for v in vendors}
        invited = sorted(draft.invited_ids(rid))
        picked = st.multiselect(
            "Invite vendors",
            list(names),
            default=invited,
            format_func=names.get,
            # keyed by who is invited now, so an invite made elsewhere (the co-pilot, Start
            # from this event) shows here instead of being undone by a stale selection
            key=f"inv_{rid}_{'-'.join(map(str, invited))}",
        )
        if set(picked) != set(invited):
            draft.set_invited(rid, picked)
            st.rerun()
        with st.popover("Add a vendor"):
            with st.form("new_vendor", border=False):
                vname = st.text_input("Name")
                vmail = st.text_input("Email")
                if st.form_submit_button("Add vendor"):
                    try:
                        draft.set_invited(rid, picked + [draft.add_vendor(vname, vmail)])
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

        subject, body = draft.render_email(rid)
        with st.expander("Email preview", expanded=False):
            st.markdown(f"**Subject:** {subject}")
            st.text(body)
            st.markdown("**Attachments**")
            labels = {"application/pdf": "Download the RFx (PDF)",
                      draft.XLSX_MIME: "Download the Excel response template (optional)"}
            try:
                files = draft.attachments(rid)
            except Exception:  # any failure building a file must not take down the page
                log.exception("Could not build the RFx attachments for event %s", rid)
                files = []
                st.warning("The attachments could not be prepared just now. Reload the page "
                           "to try again before you send.")
            for name, mime, content in files:
                st.download_button(labels.get(mime, name), content, file_name=name, mime=mime,
                                   key=f"att_{name}", help=name)
        reviewed = st.checkbox("I have reviewed the email and the draft")
        if st.button("Send to invited vendors", type="primary", disabled=not reviewed):
            try:
                n = draft.send(rid)
                st.session_state["just_sent"] = rid  # the pages stay on the current event
                st.toast(f"Sent to {n} vendors (simulated). The RFx is now issued.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if issues:
            st.caption("You can send with points still open, but replies to those lines may "
                       "not compare.")

if err := st.session_state.pop("edit_error", None):
    st.error(err)
