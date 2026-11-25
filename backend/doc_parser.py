import io
import os
from pypdf import PdfReader
from docx import Document
from pptx import Presentation

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt", ".md"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB


def extract_text_from_file(filename: str, content: bytes) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format '{ext}'. Please upload a .pptx, .docx, .pdf, .txt, or .md file."
        )

    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the 15 MB limit. Please upload a smaller presentation or document.")

    if ext in {".txt", ".md"}:
        return content.decode("utf-8", errors="ignore").strip()

    if ext == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[Page {idx}]\n{text}")
        return "\n\n".join(pages).strip()

    if ext == ".docx":
        doc = Document(io.BytesIO(content))
        blocks = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    blocks.append(row_text)
        return "\n".join(blocks).strip()

    if ext == ".pptx":
        prs = Presentation(io.BytesIO(content))
        slides_text = []
        for idx, slide in enumerate(prs.slides, start=1):
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    txt = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip())
                    if txt:
                        parts.append(txt)
                if shape.has_table:
                    for row in shape.table.rows:
                        row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                        if row_txt:
                            parts.append(row_txt)
            # Include presenter speaker notes if present
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append(f"(Speaker Notes: {notes})")
            if parts:
                slides_text.append(f"[Slide {idx}]\n" + "\n".join(parts))
        return "\n\n".join(slides_text).strip()

    return ""