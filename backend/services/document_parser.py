"""
Document parser — converts any uploaded file into raw text.

Pipeline per file type:
  Native PDF  →  pdfplumber extracts text directly
  Scanned PDF →  pdf2image converts pages to images → Claude vision reads each page
  Image file  →  Claude vision reads directly

Claude vision is the primary engine for anything non-native.
pdfplumber is used first on PDFs; if extracted text is too short (image-based PDF),
we fall back to the vision path automatically.
"""

import base64
import io
import mimetypes
from abc import ABC, abstractmethod
from pathlib import Path

import anthropic
import pdfplumber
from pdf2image import convert_from_path

from backend.config import ANTHROPIC_API_KEY, CLAUDE_MODEL

# Minimum characters from pdfplumber before we consider a PDF "native".
# Below this threshold we treat it as a scanned/image PDF.
_MIN_NATIVE_TEXT_LENGTH = 100

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MIME_TYPE = "application/pdf"


def _encode_image_bytes(image_bytes: bytes, mime_type: str) -> dict:
    """Return an Anthropic image content block from raw bytes."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
        },
    }


def _ask_claude_vision(client: anthropic.Anthropic, image_blocks: list, doc_type: str) -> str:
    """Send one or more image blocks to Claude and return the extracted text."""
    prompt = (
        f"This is a '{doc_type}' document. "
        "Extract ALL text exactly as it appears. "
        "Preserve dates, names, numbers, institutions, and addresses precisely. "
        "Do not summarise or interpret — only transcribe."
    )
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": image_blocks + [{"type": "text", "text": prompt}],
            }
        ],
    )
    return message.content[0].text


class BaseDocumentParser(ABC):
    """Abstract parser — one concrete class per file category."""

    def __init__(self, client: anthropic.Anthropic):
        self.client = client

    @abstractmethod
    def extract_text(self, file_path: str, doc_type: str) -> str:
        """Return raw extracted text from the file at file_path."""


class NativePDFParser(BaseDocumentParser):
    """Extracts text from a text-based (non-scanned) PDF using pdfplumber."""

    def extract_text(self, file_path: str, doc_type: str) -> str:
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)


class ScannedPDFParser(BaseDocumentParser):
    """
    Converts each PDF page to an image then sends to Claude vision.
    Used when pdfplumber yields too little text (image-based PDF).
    """

    def extract_text(self, file_path: str, doc_type: str) -> str:
        images = convert_from_path(file_path, dpi=200)
        image_blocks = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            image_blocks.append(_encode_image_bytes(buf.getvalue(), "image/png"))
        return _ask_claude_vision(self.client, image_blocks, doc_type)


class ImageParser(BaseDocumentParser):
    """Sends an image file directly to Claude vision for text extraction."""

    def extract_text(self, file_path: str, doc_type: str) -> str:
        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "image/jpeg"
        image_bytes = Path(file_path).read_bytes()
        image_blocks = [_encode_image_bytes(image_bytes, mime_type)]
        return _ask_claude_vision(self.client, image_blocks, doc_type)


class DocumentParserService:
    """
    Entry point for the extraction stage.

    Usage:
        service = DocumentParserService()
        text = service.parse(file_path="/uploads/abc/resume.pdf", doc_type="resume")
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    def parse(self, file_path: str, doc_type: str) -> str:
        """
        Auto-detect file type, pick the right parser, and return extracted text.
        """
        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or ""

        if mime_type in IMAGE_MIME_TYPES:
            return ImageParser(self.client).extract_text(file_path, doc_type)

        if mime_type == PDF_MIME_TYPE or file_path.lower().endswith(".pdf"):
            return self._parse_pdf(file_path, doc_type)

        # Unknown type — try reading as plain text, fallback to vision
        try:
            return Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ImageParser(self.client).extract_text(file_path, doc_type)

    def _parse_pdf(self, file_path: str, doc_type: str) -> str:
        native_text = NativePDFParser(self.client).extract_text(file_path, doc_type)
        if len(native_text.strip()) >= _MIN_NATIVE_TEXT_LENGTH:
            return native_text
        # Too little text — treat as scanned PDF
        return ScannedPDFParser(self.client).extract_text(file_path, doc_type)
