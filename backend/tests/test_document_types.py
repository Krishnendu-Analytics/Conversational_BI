"""Chat attachments: every advertised file type becomes text, and questions
sent with attachments are answered from them."""

import io
import zipfile

import pytest

from app.services.document_extractor import (
    DocumentExtractor,
    EmptyDocumentError,
    MalformedDocumentError,
    UnsupportedDocumentTypeError,
    sniff_content_type,
)


class FakeVision:
    def __init__(self, text: str = "Plant | Incidents\nHaldia | 14") -> None:
        self.text = text
        self.calls: list[str] = []

    def read_image(self, image: bytes, mime_type: str, prompt: str, max_output_tokens=None) -> str:
        self.calls.append(mime_type)
        return self.text


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.xlsx", "spreadsheetml"), ("a.xlsm", "spreadsheetml"), ("a.xls", "ms-excel"),
        ("a.docx", "wordprocessingml"), ("a.doc", "msword"), ("a.png", "image/png"),
        ("a.JPG", "image/jpeg"), ("a.jpeg", "image/jpeg"), ("a.webp", "image/webp"),
        ("a.tsv", "tab-separated"), ("a.pdf", "application/pdf"), ("a.csv", "text/csv"),
    ],
)
def test_every_advertised_extension_is_recognised(name: str, expected: str) -> None:
    assert expected in sniff_content_type(name, None)


def test_unknown_types_are_still_rejected() -> None:
    with pytest.raises(UnsupportedDocumentTypeError):
        sniff_content_type("run.exe", None)


def test_xlsx_sheets_become_tables_of_text() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Freight Rates"
    ws.append(["Route", "Mode", "Rate"])
    ws.append(["Haldia - Chennai", "Rail", 2150])
    ws.append([None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    result = DocumentExtractor().extract(buf.getvalue(), filename="f.xlsx")
    assert result.text == "## Sheet: Freight Rates\nRoute | Mode | Rate\nHaldia - Chennai | Rail | 2150"


def test_xls_is_read() -> None:
    xlwt = pytest.importorskip("xlwt")
    book = xlwt.Workbook()
    sheet = book.add_sheet("Ratings")
    sheet.write(0, 0, "Supplier")
    sheet.write(0, 1, "Rating")
    sheet.write(1, 0, "Supplier 050")
    sheet.write(1, 1, "C - on watchlist")
    buf = io.BytesIO()
    book.save(buf)
    result = DocumentExtractor().extract(buf.getvalue(), filename="s.xls")
    assert "Supplier 050 | C - on watchlist" in result.text


def test_docx_paragraphs_and_tables() -> None:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = (
        f'<w:document xmlns:w="{w}"><w:body>'
        "<w:p><w:r><w:t>Shutdown plan</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Unit</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Days</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Cracker</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>15</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    result = DocumentExtractor().extract(buf.getvalue(), filename="plan.docx")
    assert result.text == "Shutdown plan\nUnit | Days\nCracker | 15"


def test_broken_word_files_fail_clearly() -> None:
    with pytest.raises(MalformedDocumentError):
        DocumentExtractor().extract(b"not a zip", filename="x.docx")
    with pytest.raises(MalformedDocumentError):
        DocumentExtractor().extract(b"not an ole file", filename="x.doc")


def test_images_are_transcribed_by_the_vision_model() -> None:
    vision = FakeVision()
    result = DocumentExtractor(llm=vision).extract(_png(), filename="table.png")
    assert result.text == "Plant | Incidents\nHaldia | 14"
    assert vision.calls == ["image/png"]


def test_image_without_llm_key_is_an_error_not_a_stub() -> None:
    class NoKey:
        def read_image(self, *a, **k):
            raise RuntimeError("Reading images needs LLM_API_KEY")

    with pytest.raises(MalformedDocumentError, match="LLM_API_KEY"):
        DocumentExtractor(llm=NoKey()).extract(_png(), filename="x.jpg")


def test_scanned_pdf_pages_are_read_by_the_vision_model() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, format="PDF")
    vision = FakeVision("Aromatics Unit | 2 lost-time injuries")
    result = DocumentExtractor(llm=vision).extract(buf.getvalue(), filename="scan.pdf")
    assert result.text == "[Page 1]\nAromatics Unit | 2 lost-time injuries"
    assert vision.calls  # the page image went to the vision model

    blank = FakeVision("")
    with pytest.raises(EmptyDocumentError):
        DocumentExtractor(llm=blank).extract(buf.getvalue(), filename="scan.pdf")


def test_questions_with_attachments_always_search_them() -> None:
    from app.agent.workflow import _route_with_attachments

    assert _route_with_attachments("sql", ["d1"]) == "hybrid"
    assert _route_with_attachments("greeting", ["d1"]) == "rag"
    assert _route_with_attachments("rag", ["d1"]) == "rag"
    assert _route_with_attachments("dashboard", ["d1"]) == "dashboard"
    assert _route_with_attachments("sql", []) == "sql"
    assert _route_with_attachments("sql", None) == "sql"


def test_attached_answer_prefers_documents_and_clears_no_data(monkeypatch) -> None:
    from app.agent import workflow

    prompts: list[str] = []

    class LLM:
        def complete(self, prompt: str, max_output_tokens=None) -> str:
            prompts.append(prompt)
            return "Industrial Supplier 050 is on the watchlist. [Source 1]"

    monkeypatch.setattr(workflow, "get_llm", lambda: LLM())
    state = {
        "intent": "hybrid",
        "question": "Which supplier is on the watchlist?",
        "attached_document_ids": ["d1"],
        "rag_chunks": [{"content": "Supplier 050 | C - on watchlist"}],
        "rag_context": "[Source 1] Supplier 050 | C - on watchlist",
        "rag_insufficient": False,
        "query_clarification": {"kind": "invalid_plan", "message": "no data", "choices": []},
        "answer": "This connection's schema doesn't appear to contain data about that.",
        "rows": [],
        "steps": [],
    }
    update = workflow.InsightGenerator().run(state)
    assert update["answer"] == "Industrial Supplier 050 is on the watchlist. [Source 1]"
    assert update["query_clarification"] is None
    assert "(the database has no data about this)" in prompts[0]
