"""Extract plain text from uploaded documents (no code execution).

Supported: plain text (.txt .md .csv .tsv .json), PDF (text, and scanned
pages via the vision model), Excel (.xlsx .xlsm .xls), Word (.docx, and
best-effort legacy .doc), and images (.png .jpg .jpeg .webp — transcribed
by the configured vision-capable LLM). Everything becomes text, which the
RAG pipeline then chunks and embeds.

Nothing in a file is executed: spreadsheets are read with formulas'
cached values (``data_only``), Word files are parsed as XML or binary
streams, and images are only described.
"""

from __future__ import annotations

import io
import logging
import re
import struct
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


class UnsupportedDocumentTypeError(ValueError):
    code = "unsupported_document_type"


class EmptyDocumentError(ValueError):
    code = "empty_document"


class MalformedDocumentError(ValueError):
    code = "malformed_document"


_TEXT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/tab-separated-values",
    "application/json",
}
_PDF_TYPES = {"application/pdf"}
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLS = "application/vnd.ms-excel"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOC = "application/msword"
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}

_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".xlsx": _XLSX,
    ".xlsm": _XLSX,
    ".xls": _XLS,
    ".docx": _DOCX,
    ".doc": _DOC,
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_SUPPORTED_LABEL = ".pdf, .csv, .tsv, .txt, .md, .json, .xlsx, .xlsm, .xls, .docx, .doc, .png, .jpg, .jpeg, .webp"

# Spreadsheets can be huge; the RAG store keeps a bounded number of chunks
# anyway, so stop reading well before that.
_MAX_SHEET_ROWS = 20_000
_MAX_CELL_CHARS = 300
# Scanned PDFs: pages sent to the vision model.
_MAX_SCANNED_PAGES = 20
_MAX_IMAGE_SIDE = 2000

_IMAGE_PROMPT = (
    "Transcribe this image for a business document search index.\n"
    "- Copy all visible text exactly, in reading order.\n"
    "- Reproduce tables as Markdown tables with their headers and every value.\n"
    "- For charts, state the title, axes, series and the values you can read.\n"
    "- For photos or diagrams without text, describe what they show in 2-3 sentences.\n"
    "Do not add commentary or information that is not in the image."
)


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    content_type: str
    page_count: int | None = None


def sniff_content_type(filename: str, declared: str | None) -> str:
    from pathlib import Path

    ext = Path(filename).suffix.lower()
    if ext in _EXTENSIONS:
        return _EXTENSIONS[ext]
    known = _TEXT_TYPES | _PDF_TYPES | _IMAGE_TYPES | {_XLSX, _XLS, _DOCX, _DOC}
    if declared and declared.split(";")[0].strip().lower() in known:
        return declared.split(";")[0].strip().lower()
    raise UnsupportedDocumentTypeError(
        f"Unsupported document type for '{filename}'. Supported: {_SUPPORTED_LABEL}"
    )


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:_MAX_CELL_CHARS]


def _rows_to_text(title: str, rows: list[list[Any]]) -> tuple[str, int]:
    """One sheet as '## Sheet: name' + ' | '-separated lines (header first)."""
    lines: list[str] = []
    for row in rows:
        cells = [_cell(c) for c in row]
        while cells and not cells[-1]:
            cells.pop()
        if any(cells):
            lines.append(" | ".join(cells))
    if not lines:
        return "", 0
    return f"## Sheet: {title}\n" + "\n".join(lines), len(lines)


class DocumentExtractor:
    """Read text from supported uploads."""

    def __init__(self, llm: Any | None = None) -> None:
        # Vision-capable LLM for images / scanned PDFs (lazy: get_llm()).
        self._llm = llm

    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> ExtractionResult:
        ctype = sniff_content_type(filename, content_type)
        if ctype in _TEXT_TYPES or ctype.startswith("text/"):
            return self._extract_text(content, ctype)
        if ctype in _PDF_TYPES:
            return self._extract_pdf(content)
        if ctype == _XLSX:
            return self._extract_xlsx(content)
        if ctype == _XLS:
            return self._extract_xls(content)
        if ctype == _DOCX:
            return self._extract_docx(content)
        if ctype == _DOC:
            return self._extract_doc(content)
        if ctype in _IMAGE_TYPES:
            return self._extract_image(content, ctype)
        raise UnsupportedDocumentTypeError(f"Unsupported content type: {ctype}")

    # --- Plain text ------------------------------------------------------------------

    def _extract_text(self, content: bytes, ctype: str) -> ExtractionResult:
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise MalformedDocumentError("Could not decode text document")
        text = text.strip()
        if not text:
            raise EmptyDocumentError("Document contains no text")
        return ExtractionResult(text=text, content_type=ctype)

    # --- PDF ----------------------------------------------------------------------------

    def _extract_pdf(self, content: bytes) -> ExtractionResult:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise MalformedDocumentError("PDF support is not installed") from exc

        try:
            reader = PdfReader(io.BytesIO(content))
            parts: list[str] = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    parts.append(page_text.strip())
        except Exception as exc:  # noqa: BLE001
            raise MalformedDocumentError(f"Could not read PDF: {type(exc).__name__}") from exc

        text = "\n\n".join(parts).strip()
        if not text:
            # A scanned PDF: pages are images. Read them with the vision model.
            text = self._read_scanned_pdf(reader)
        if not text:
            raise EmptyDocumentError("PDF contains no extractable text")
        return ExtractionResult(
            text=text,
            content_type="application/pdf",
            page_count=len(reader.pages),
        )

    def _read_scanned_pdf(self, reader: Any) -> str:
        parts: list[str] = []
        for number, page in enumerate(reader.pages[:_MAX_SCANNED_PAGES], start=1):
            try:
                images = list(page.images)
            except Exception:  # noqa: BLE001 — image decoding needs Pillow for most filters
                logger.info("scanned_pdf_page_images_unreadable page=%s", number)
                continue
            page_parts = []
            for image in images:
                data = getattr(image, "data", None)
                if not data:
                    continue
                png = _normalise_image(data)
                if png is None:
                    continue
                page_parts.append(self._vision(png, "image/png").strip())
            page_text = "\n".join(p for p in page_parts if p)
            if page_text:
                parts.append(f"[Page {number}]\n{page_text}")
        return "\n\n".join(parts).strip()

    # --- Excel --------------------------------------------------------------------------

    def _extract_xlsx(self, content: bytes) -> ExtractionResult:
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise MalformedDocumentError("Could not read the Excel file") from exc
        sections: list[str] = []
        budget = _MAX_SHEET_ROWS
        try:
            for ws in wb.worksheets:
                if budget <= 0:
                    break
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append(list(row))
                    if len(rows) >= budget:
                        break
                text, used = _rows_to_text(str(ws.title), rows)
                budget -= used
                if text:
                    sections.append(text)
        finally:
            wb.close()
        return self._sheets_result(sections, _XLSX)

    def _extract_xls(self, content: bytes) -> ExtractionResult:
        try:
            import xlrd
        except ImportError as exc:
            raise MalformedDocumentError("Old Excel (.xls) support is not installed") from exc
        try:
            book = xlrd.open_workbook(file_contents=content)
        except Exception as exc:  # noqa: BLE001
            raise MalformedDocumentError("Could not read the .xls file") from exc
        sections: list[str] = []
        budget = _MAX_SHEET_ROWS
        for sheet in book.sheets():
            if budget <= 0:
                break
            rows = [sheet.row_values(i) for i in range(min(sheet.nrows, budget))]
            text, used = _rows_to_text(sheet.name, rows)
            budget -= used
            if text:
                sections.append(text)
        return self._sheets_result(sections, _XLS)

    @staticmethod
    def _sheets_result(sections: list[str], ctype: str) -> ExtractionResult:
        text = "\n\n".join(sections).strip()
        if not text:
            raise EmptyDocumentError("Spreadsheet contains no data")
        return ExtractionResult(text=text, content_type=ctype, page_count=len(sections))

    # --- Word ---------------------------------------------------------------------------

    def _extract_docx(self, content: bytes) -> ExtractionResult:
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        w = "{%s}" % ns["w"]
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                root = ElementTree.fromstring(zf.read("word/document.xml"))
        except Exception as exc:  # noqa: BLE001
            raise MalformedDocumentError("Could not read the Word (.docx) file") from exc

        def para_text(p: ElementTree.Element) -> str:
            out = []
            for node in p.iter():
                if node.tag == f"{w}t" and node.text:
                    out.append(node.text)
                elif node.tag == f"{w}tab":
                    out.append("\t")
                elif node.tag in (f"{w}br", f"{w}cr"):
                    out.append("\n")
            return "".join(out).strip()

        lines: list[str] = []
        body = root.find("w:body", ns)
        for block in list(body) if body is not None else []:
            if block.tag == f"{w}p":
                text = para_text(block)
                if text:
                    lines.append(text)
            elif block.tag == f"{w}tbl":
                for tr in block.iter(f"{w}tr"):
                    cells = [
                        " ".join(para_text(p) for p in tc.iter(f"{w}p")).strip()
                        for tc in tr.iter(f"{w}tc")
                    ]
                    if any(cells):
                        lines.append(" | ".join(cells))
                lines.append("")
        text = "\n".join(lines).strip()
        if not text:
            raise EmptyDocumentError("Word document contains no text")
        return ExtractionResult(text=text, content_type=_DOCX)

    def _extract_doc(self, content: bytes) -> ExtractionResult:
        """Legacy Word 97-2003: read the text through the piece table.

        Best effort: formatting, headers and embedded objects are ignored."""
        try:
            import olefile
        except ImportError as exc:
            raise MalformedDocumentError("Old Word (.doc) support is not installed") from exc
        try:
            ole = olefile.OleFileIO(io.BytesIO(content))
        except Exception as exc:  # noqa: BLE001
            raise MalformedDocumentError("Could not read the Word (.doc) file") from exc
        try:
            text = _doc_piece_table_text(ole) or _doc_heuristic_text(ole)
        finally:
            ole.close()
        text = _clean_doc_text(text)
        if not text:
            raise EmptyDocumentError(
                "No text could be read from this .doc file; save it as .docx or PDF and upload again"
            )
        return ExtractionResult(text=text, content_type=_DOC)

    # --- Images --------------------------------------------------------------------------

    def _extract_image(self, content: bytes, ctype: str) -> ExtractionResult:
        image, mime = content, ctype
        normalised = _normalise_image(content)
        if normalised is not None:
            image, mime = normalised, "image/png"
        text = self._vision(image, mime).strip()
        if not text:
            raise EmptyDocumentError("Nothing could be read from this image")
        return ExtractionResult(text=text, content_type=ctype, page_count=1)

    def _vision(self, image: bytes, mime: str) -> str:
        llm = self._llm
        if llm is None:
            from app.llm.client import get_llm

            llm = get_llm()
        try:
            return llm.read_image(image, mime, _IMAGE_PROMPT, max_output_tokens=4000)
        except RuntimeError as exc:
            raise MalformedDocumentError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise MalformedDocumentError(
                f"The vision model could not read this image ({type(exc).__name__})"
            ) from exc


# --- Helpers ------------------------------------------------------------------------------


def _normalise_image(data: bytes) -> bytes | None:
    """Any image Pillow can open -> PNG, longest side capped. None if not an image."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.mode not in ("RGB", "L", "RGBA"):
                img = img.convert("RGB")
            if max(img.size) > _MAX_IMAGE_SIDE:
                img.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE))
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _doc_piece_table_text(ole: Any) -> str:
    """Text of a Word 97+ document via its CLX piece table ([MS-DOC] 2.9.38)."""
    try:
        word = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", word, 0x000A)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            return ""
        table = ole.openstream(table_name).read()
        fc_clx, lcb_clx = struct.unpack_from("<II", word, 0x01A2)
        clx = table[fc_clx : fc_clx + lcb_clx]
        pos = 0
        while pos < len(clx) and clx[pos] == 0x01:  # skip Prc (property modifiers)
            cb = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + cb
        if pos >= len(clx) or clx[pos] != 0x02:
            return ""
        lcb = struct.unpack_from("<I", clx, pos + 1)[0]
        plc = clx[pos + 5 : pos + 5 + lcb]
        n = (len(plc) - 4) // 12  # (n+1) CPs of 4 bytes + n PCDs of 8 bytes
        cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
        out: list[str] = []
        for i in range(n):
            fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
            chars = cps[i + 1] - cps[i]
            if fc & 0x40000000:  # compressed: 8-bit cp1252
                start = (fc & ~0x40000000) // 2
                out.append(word[start : start + chars].decode("cp1252", errors="replace"))
            else:
                out.append(word[fc : fc + 2 * chars].decode("utf-16-le", errors="replace"))
        return "".join(out)
    except Exception:  # noqa: BLE001 — fall back to the heuristic reader
        return ""


def _doc_heuristic_text(ole: Any) -> str:
    """Runs of readable text in the WordDocument stream (last resort)."""
    try:
        data = ole.openstream("WordDocument").read()
    except Exception:  # noqa: BLE001
        return ""
    wide = re.findall(rb"(?:[\x20-\x7e\r\n\t]\x00){8,}", data)
    narrow = re.findall(rb"[\x20-\x7e\r\n\t]{8,}", data)
    wide_text = "\n".join(w.decode("utf-16-le", errors="ignore") for w in wide)
    narrow_text = "\n".join(n.decode("cp1252", errors="ignore") for n in narrow)
    return wide_text if len(wide_text) >= len(narrow_text) else narrow_text


def _clean_doc_text(text: str) -> str:
    # Word uses \r for paragraph ends, \x07 for cell/row ends, \x13-\x15 for fields.
    text = text.replace("\r", "\n").replace("\x07", " | ").replace("\x0b", "\n")
    text = re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
