"""File upload storage + conversion of stored files into Claude content blocks."""
import base64
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from ..config import get_settings

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
IMAGE_LIMIT_MB = 5  # API limit per image


def save_upload(upload: UploadFile, subdir: str) -> str:
    """Store an upload under data/uploads/<subdir>/ and return the path relative to data_dir."""
    settings = get_settings()
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in MIME_BY_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(MIME_BY_EXT))}")
    limit_mb = settings.max_upload_mb if ext == ".pdf" else min(IMAGE_LIMIT_MB, settings.max_upload_mb)
    data = upload.file.read(limit_mb * 1024 * 1024 + 1)
    if len(data) > limit_mb * 1024 * 1024:
        hint = "" if ext == ".pdf" else " (compress the photo or combine pages into a PDF)"
        raise HTTPException(413, f"'{upload.filename}' exceeds {limit_mb} MB{hint}")
    if not data:
        raise HTTPException(400, f"'{upload.filename}' is empty")
    dest_dir = settings.uploads_dir / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}{ext}"  # never trust the client filename
    dest.write_bytes(data)
    return dest.relative_to(settings.data_dir).as_posix()


def resolve(rel_path: str) -> Path:
    settings = get_settings()
    p = (settings.data_dir / rel_path).resolve()
    if settings.data_dir not in p.parents:
        raise ValueError("Path escapes data directory")
    return p


def build_file_blocks(rel_paths: list[str]) -> list[dict]:
    """Convert stored PDFs/images to Anthropic content blocks (base64)."""
    blocks: list[dict] = []
    for rel in rel_paths:
        p = resolve(rel)
        mime = MIME_BY_EXT.get(p.suffix.lower())
        if mime is None or not p.exists():
            raise FileNotFoundError(f"Missing or unsupported file: {rel}")
        data = base64.standard_b64encode(p.read_bytes()).decode("ascii")
        kind = "document" if mime == "application/pdf" else "image"
        blocks.append({"type": kind, "source": {"type": "base64", "media_type": mime, "data": data}})
    return blocks
