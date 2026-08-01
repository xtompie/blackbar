"""Reading PDFs locally so their text can be redacted like any other text.

Claude Code sends a PDF as base64 and Anthropic parses it server-side, which would put
the whole document outside the machine untouched. Instead we extract the text here,
hand it to the same redaction path as everything else, and send text.

What the model loses: page layout, tables as tables, and anything that is only a picture.
A scanned PDF has no text layer at all - there is nothing to extract, so it is treated
like an image and refused.
"""

from __future__ import annotations

import base64
import io


def extract_text(data_b64: str) -> str | None:
    """Returns the text of a PDF, or None when there is nothing to read."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        raw = base64.b64decode(data_b64)
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return None
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    return text or None
