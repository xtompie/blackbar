"""Turning attachments into text so they can be redacted like anything else.

Claude Code sends files as base64 and Anthropic parses them server-side, which would
put whole documents outside the machine untouched. The rule here is the same as for
endpoints: what we can read, we read and redact; what we cannot read does not go out.

A type can be allowed through explicitly in the config - it then travels exactly as it
is, unredacted, and `blackbar status` says so.
"""

from __future__ import annotations

import base64
import io
import re
import zipfile


def _from_pdf(raw: bytes) -> str | None:
    try:
        from pypdf import PdfReader

        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages]
    except Exception:
        return None
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    return text or None


def _from_plain(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            return None
    return text or None


def _from_docx(raw: bytes) -> str | None:
    """Word files are a zip with XML inside - no extra dependency needed."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except Exception:
        return None
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml).strip()
    return text or None


# media type -> reader. Everything here is extracted, redacted and sent as text.
EXTRACTORS = {
    "application/pdf": _from_pdf,
    "application/json": _from_plain,
    "application/xml": _from_plain,
    "application/x-yaml": _from_plain,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _from_docx,
}

# whole families handled as text
TEXT_PREFIXES = ("text/",)


# What to show a human. The raw media types are exact but unreadable in a status screen.
LABELS = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word (.docx)",
    "application/json": "JSON",
    "application/xml": "XML",
    "application/x-yaml": "YAML",
}


def supported_types() -> list[str]:
    """Media types we can read, as labels."""
    return ["text/* (txt, csv, md, html)", *sorted(LABELS.values())]


def reader_for(media_type: str):
    if media_type in EXTRACTORS:
        return EXTRACTORS[media_type]
    if media_type.startswith(TEXT_PREFIXES):
        return _from_plain
    return None


def extract(media_type: str, data_b64: str) -> str | None:
    """Returns the text of an attachment, or None when there is nothing to read.

    None also covers a scanned PDF: a picture in a document wrapper, with no text layer.
    """
    reader = reader_for(media_type)
    if reader is None:
        return None
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    return reader(raw)
