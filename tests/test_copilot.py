"""WP5: the RFx draft store and the co-pilot loop. The model is faked: no API calls."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from level import db, llm
from level.agents.copilot import agent, draft, rfx_doc
from level.agents.vendor_copilot.link import link_url


@pytest.fixture
def rfx(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    old = draft.new_draft("Last year")
    draft.upsert_lines(
        old,
        [
            {
                "line_no": 1,
                "description": "Widget",
                "canonical_uom": "each",
                "annual_qty": "1,000",
                "spec": {"size": "M"},
            },
            {
                "line_no": 2,
                "description": "Bolts",
                "canonical_uom": "dozen",
                "annual_qty": "50",
                "spec": {"thread": "M8"},
                "conversion_factors": {"pieces_per_dozen": "12"},
            },
        ],
    )
    with db.begin() as conn:
        conn.execute(db.rfx.update().where(db.rfx.c.id == old).values(status="awarded"))
        vid = conn.execute(
            db.vendor.insert().values(name="Acme", email="a@x")
        ).inserted_primary_key[0]
    new = draft.new_draft("This year")
    return SimpleNamespace(old=old, new=new, vendor=vid)


def test_import_from_history_copies_lines(rfx):
    out = draft.import_from_history(rfx.new)
    lines = draft.snapshot(rfx.new)["lines"]
    assert out["imported"] == 2 and lines[1]["conversion_factors"] == {"pieces_per_dozen": "12"}
    assert lines[0]["annual_qty"] == "1000"  # stored as a clean decimal string


def test_partial_update_keeps_other_fields(rfx):
    """A chat edit to one field must not wipe a direct edit to another."""
    draft.import_from_history(rfx.new)
    draft.upsert_lines(rfx.new, [{"line_no": 1, "plant": "North"}], actor="buyer")
    draft.upsert_lines(rfx.new, [{"line_no": 1, "annual_qty": "2000"}], actor="copilot")
    line = draft.snapshot(rfx.new)["lines"][0]
    assert (line["plant"], line["annual_qty"], line["spec"]) == ("North", "2000", {"size": "M"})


def test_completeness_names_what_is_missing(rfx):
    draft.upsert_lines(rfx.new, [{"description": "Gadget", "canonical_uom": "each"}])
    draft.upsert_questions(rfx.new, [{"text": "Certified?", "section": "Q", "is_gating": True}])
    issues = " ".join(draft.check_completeness(rfx.new))
    for expected in (
        "no annual quantity",
        "no specification",
        "no pass rule",
        "payment terms (days) is not stated",
        "No vendors invited",
    ):
        assert expected in issues


def test_complete_draft_has_no_issues(rfx):
    draft.import_from_history(rfx.new)
    draft.set_terms(
        rfx.new,
        {
            "price_basis": "delivered",
            "currency": "INR",
            "payment_days": "60",
            "delivery": "to site",
            "due": "2026-12-01",
        },
    )
    draft.upsert_questions(
        rfx.new,
        [
            {
                "text": "Lead time?",
                "section": "C",
                "is_gating": True,
                "pass_rule": {"op": "<=", "value": "10"},
            }
        ],
    )
    draft.set_invited(rfx.new, [rfx.vendor])
    assert draft.check_completeness(rfx.new) == []


def test_send_needs_click_and_happens_once(rfx):
    draft.import_from_history(rfx.new)
    draft.set_terms(rfx.new, {"due": "2026-12-01"})
    with pytest.raises(ValueError):
        draft.send(rfx.new)  # nobody invited
    draft.set_invited(rfx.new, [rfx.vendor])
    assert draft.send(rfx.new) == 1
    assert draft.send(rfx.new) == 0  # a double-click sends nothing twice
    with db.connect() as conn:
        out = conn.execute(sa.select(db.message).where(db.message.c.direction == "out")).all()
        atts = conn.execute(sa.select(db.attachment)).all()
    assert len(out) == 1 and len(atts) == 2
    assert sorted(a.mime for a in atts) == ["application/pdf", draft.XLSX_MIME]
    assert link_url(rfx.new, rfx.vendor) in out[0].body  # the vendor's own link, in the body
    assert draft.LINK_PLACEHOLDER not in out[0].body
    assert draft.snapshot(rfx.new)["status"] == "issued"


def _complete(rfx):
    draft.import_from_history(rfx.new)
    draft.upsert_lines(rfx.new, [{"line_no": 1, "plant": "North yard"}])
    draft.set_terms(rfx.new, {"price_basis": "delivered", "currency": "INR",
                              "payment_days": "60", "delivery": "to site", "due": "2026-12-01"})
    draft.upsert_questions(rfx.new, [{"text": "Lead time in days?", "section": "Supply",
                                      "is_gating": True}])


def test_email_body_is_short_and_points_to_the_attached_rfx(rfx):
    _complete(rfx)
    subject, body = draft.render_email(rfx.new, "https://example.test/?t=abc")
    assert subject == "Request for quotation: This year"
    assert "Widget" not in body and "Bolts" not in body  # lines live in the attachment
    assert "Lead time" not in body  # so does the questionnaire
    assert "1 Dec 2026" in body and "attached RFx" in body
    assert "https://example.test/?t=abc" in body
    assert body.index("https://example.test") < body.index("Regards")  # link before sign-off
    assert len(body.splitlines()) <= 20


def test_rfx_pdf_holds_lines_specs_terms_and_questionnaire(rfx):
    _complete(rfx)
    names = [name for name, _, _ in draft.attachments(rfx.new)]
    assert names == [rfx_doc.filename(rfx.new), draft.TEMPLATE_NAME]
    assert names[0].endswith(".pdf") and "/" not in names[0]
    text = _pdf_text(rfx_doc.rfx_pdf(rfx.new))
    for expected in ("Widget", "Bolts", "Size: M", "Thread: M8", "1,000", "dozen",
                     "North yard", "Payment terms", "60", "Lead time in days?", "1 Dec 2026"):
        assert expected in text, expected


def test_rfx_pdf_is_built_in_memory(rfx, tmp_path, monkeypatch):
    """No temp file: on Windows one still held open by the writer cannot be deleted."""
    import tempfile

    def no_temp(*args, **kwargs):
        raise AssertionError("rfx_pdf must not create a temp file")

    for name in ("mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryFile"):
        monkeypatch.setattr(tempfile, name, no_temp)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    content = rfx_doc.rfx_pdf(rfx.new)
    assert isinstance(content, bytes) and content.startswith(b"%PDF")
    assert list((tmp_path / "tmp").iterdir()) == []


def _pdf_text(content: bytes) -> str:
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return " ".join(" ".join(page.get_text().split()) for page in doc)


def test_editor_helpers_round_trip(rfx):
    assert draft.parse_pairs(draft.format_pairs({"a": "1", "b": "x y"})) == {"a": "1", "b": "x y"}
    assert draft.parse_rule("<= 10") == {"op": "<=", "value": "10"}
    assert draft.parse_rule("is_yes") == {"op": "is_yes", "value": None}
    with pytest.raises(ValueError):
        draft.parse_rule("about 10")
    with pytest.raises(ValueError):
        draft.parse_pairs("no equals sign")


def test_apply_line_edits_changes_only_edited_cells(rfx):
    draft.import_from_history(rfx.new)
    rows = draft.snapshot(rfx.new)["lines"]
    draft.apply_line_edits(
        rfx.new,
        rows,
        {0: {"spec": "size=L; colour=red"}},
        [{"description": "New thing", "canonical_uom": "each"}],
        [1],
    )
    lines = draft.snapshot(rfx.new)["lines"]
    assert [(x["line_no"], x["description"]) for x in lines] == [(1, "Widget"), (2, "New thing")]
    assert lines[0]["spec"] == {"size": "L", "colour": "red"} and lines[0]["annual_qty"] == "1000"


def test_tools_are_strict_and_cannot_send():
    names = {t["name"] for t in agent.TOOLS}
    assert names == {
        "import_from_history",
        "upsert_lines",
        "remove_lines",
        "set_terms",
        "upsert_questions",
        "check_completeness",
        "preview_email",
    }
    assert all(
        t["strict"] and t["input_schema"]["additionalProperties"] is False for t in agent.TOOLS
    )


def _fake_message(blocks, stop):
    return SimpleNamespace(content=blocks, stop_reason=stop)


def test_run_turn_executes_tools_and_saves_thread(rfx, monkeypatch):
    """Round 1: the model calls two tools. Round 2: it answers. Both rounds use llm.complete."""
    calls = []
    script = [
        _fake_message(
            [
                SimpleNamespace(type="tool_use", id="t1", name="import_from_history", input={}),
                SimpleNamespace(
                    type="tool_use",
                    id="t2",
                    name="set_terms",
                    input={"terms": [{"key": "payment_days", "value": "60"}]},
                ),
            ],
            "tool_use",
        ),
        _fake_message(
            [SimpleNamespace(type="text", text="Imported 2 lines. Which currency?")], "end_turn"
        ),
    ]

    def fake_complete(messages, **kwargs):
        calls.append(list(messages))  # copy: the agent keeps appending
        msg = script[len(calls) - 1]
        text = "".join(b.text for b in msg.content if b.type == "text")
        return SimpleNamespace(message=msg, text=text)

    monkeypatch.setattr(llm, "complete", fake_complete)
    reply = agent.run_turn(rfx.new, "repeat the previous event's lines, 60 days")
    assert reply == "Imported 2 lines. Which currency?"
    assert len(draft.snapshot(rfx.new)["lines"]) == 2
    # the copied event states no terms; its comparison currency comes with it
    assert draft.snapshot(rfx.new)["terms"] == {"payment_days": "60", "currency": "INR"}
    # both tool results went back in ONE user message
    results = calls[1][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    # the draft snapshot is in the first request, so direct edits are always seen
    assert "Current draft" in calls[0][-1]["content"][0]["text"]
    hist = agent.history(agent.thread_for(rfx.new))
    assert [h["role"] for h in hist] == ["user", "assistant"]
    with db.connect() as conn:
        assert len(conn.execute(sa.select(db.tool_call)).all()) == 2


def test_tool_error_is_returned_not_raised(rfx, monkeypatch):
    script = [
        _fake_message(
            [
                SimpleNamespace(
                    type="tool_use",
                    id="t1",
                    name="upsert_lines",
                    input={
                        "lines": [
                            {
                                "line_no": None,
                                "description": None,
                                "canonical_uom": None,
                                "annual_qty": None,
                                "plant": None,
                                "spec": None,
                                "conversion_factors": None,
                            }
                        ]
                    },
                )
            ],
            "tool_use",
        ),
        _fake_message([SimpleNamespace(type="text", text="What is the item?")], "end_turn"),
    ]
    seen = []

    def fake_complete(messages, **kwargs):
        seen.append(list(messages))
        msg = script[len(seen) - 1]
        return SimpleNamespace(
            message=msg, text="".join(b.text for b in msg.content if b.type == "text")
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    agent.run_turn(rfx.new, "add a line")
    result = seen[1][-1]["content"][0]
    assert result["is_error"] and "needs a description" in result["content"]
