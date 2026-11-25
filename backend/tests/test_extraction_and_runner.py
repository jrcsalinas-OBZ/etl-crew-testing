import json
import sys
import types

import pytest

from app.services.extraction import extract_text
from app.services.runner import AnthropicRunner, RunInput, RunnerError, to_api_messages
from tests.test_chat_sessions import make_docx, make_xlsx


def test_docx_text_and_tables_are_extracted():
    from docx import Document
    import io

    doc = Document()
    doc.add_paragraph("Job inventory")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "job", "schedule"
    table.cell(1, 0).text, table.cell(1, 1).text = "vlr_nokia_dly", "daily"
    buf = io.BytesIO()
    doc.save(buf)
    result = extract_text("inv.docx", buf.getvalue())
    assert result.ok and "Job inventory" in result.text and "vlr_nokia_dly | daily" in result.text


def test_xlsx_rows_are_tab_separated_with_sheet_name():
    result = extract_text("jobs.xlsx", make_xlsx([["a", "b"], [1, 2]]))
    assert result.ok and "## Sheet: Jobs" in result.text and "a\tb" in result.text


def test_notebook_cells_are_extracted():
    nb = json.dumps({"cells": [{"cell_type": "markdown", "source": ["# Title"]},
                               {"cell_type": "code", "source": ["print(1)"]}]}).encode()
    assert "[code]\nprint(1)" in extract_text("n.ipynb", nb).text


@pytest.mark.parametrize("name,content,fragment", [
    ("broken.pdf", b"%PDF-not-really", "could not read"),
    ("broken.docx", b"not a zip", "could not read"),
    ("old.doc", b"\xd0\xcf\x11\xe0", "legacy Office format"),
    ("empty.txt", b"  \n", "empty"),
    ("noext", b"abc", "unsupported"),
])
def test_unreadable_files_report_a_reason_instead_of_raising(name, content, fragment):
    result = extract_text(name, content)
    assert not result.ok and fragment in result.note


def test_messages_merge_same_role_start_with_user_and_cache_documents():
    run = RunInput(system="s", documents="<document name=\"a\">x</document>", turns=[
        ("assistant", "hello"), ("user", "one"), ("user", "two"), ("assistant", "ok"), ("user", "three")])
    messages = to_api_messages(run)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    first = messages[0]["content"]
    assert first[0]["cache_control"] == {"type": "ephemeral"} and first[1]["text"] == "one\n\ntwo"


def test_no_user_turn_is_an_error():
    with pytest.raises(RunnerError):
        to_api_messages(RunInput(system="s", documents="", turns=[("assistant", "hi")]))


def test_anthropic_runner_streams_with_documented_parameters(monkeypatch):
    captured = {}

    class FakeStream:
        text_stream = iter(["Hello ", "world"])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return types.SimpleNamespace(stop_reason="end_turn")

    class FakeClient:
        beta = types.SimpleNamespace(messages=types.SimpleNamespace(
            stream=lambda **kwargs: captured.update(kwargs) or FakeStream()))

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClient())
    out = "".join(AnthropicRunner("claude-opus-5-5").stream(RunInput(system="sys", documents="", turns=[("user", "hi")])))
    assert out == "Hello world"
    assert captured["model"] == "claude-opus-5-5" and captured["system"] == "sys"
    assert captured["betas"] == ["server-side-fallback-2026-07-01"]
    assert captured["extra_body"] == {"fallbacks": "default"}


def test_refusal_is_surfaced_as_an_error(monkeypatch):
    class FakeStream:
        text_stream = iter([])
        __enter__ = lambda self: self
        __exit__ = lambda self, *exc: False
        get_final_message = lambda self: types.SimpleNamespace(stop_reason="refusal")

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda: types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: FakeStream()))))
    with pytest.raises(RunnerError, match="declined"):
        list(AnthropicRunner("m").stream(RunInput(system="s", documents="", turns=[("user", "hi")])))
