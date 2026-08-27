import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from uuid import UUID, uuid5
import zipfile

from docx import Document
from fastapi import UploadFile
from pypdf import PdfReader


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_DOCX_ENTRIES = 1_000
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_NORMALIZED_CHARS = 1_000_000
MAX_CHUNK_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
_CHUNK_NAMESPACE = UUID("c3e379b4-a7a3-5d83-aaf9-bded3788c429")
_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_SENTENCE = re.compile(r".*?(?:[。！？!?](?:\s+|$)|\.(?:\s+|$)|$)")


@dataclass(frozen=True)
class ValidatedKnowledgeUpload:
    original_filename: str
    mime_type: str
    sha256: str
    data: bytes


@dataclass(frozen=True)
class StoredKnowledgeUpload(ValidatedKnowledgeUpload):
    storage_path: Path


@dataclass(frozen=True)
class ChunkDraft:
    chunk_index: int
    chunk_id: str
    chunk_hash: str
    canonical_text: str
    chunk_metadata: dict[str, object]
    token_count: int


@dataclass(frozen=True)
class _Paragraph:
    text: str
    heading_path: tuple[str, ...]
    page_number: int | None
    paragraph_index: int


class KnowledgeContentError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def read_and_validate_upload(upload: UploadFile) -> ValidatedKnowledgeUpload:
    filename = _validate_client_filename(upload.filename)
    suffix = filename.suffix.lower()
    expected_mime = _MIME_BY_SUFFIX.get(suffix)
    if expected_mime is None:
        raise KnowledgeContentError("KNOWLEDGE_FILE_TYPE_INVALID")
    mime_type = (upload.content_type or "").partition(";")[0].lower()
    if mime_type != expected_mime:
        raise KnowledgeContentError("KNOWLEDGE_MIME_MISMATCH")

    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise KnowledgeContentError("KNOWLEDGE_FILE_EMPTY")
    if len(data) > MAX_UPLOAD_BYTES:
        raise KnowledgeContentError("KNOWLEDGE_FILE_TOO_LARGE")

    paragraphs, character_count = _source_paragraphs(
        filename, mime_type, data, "KNOWLEDGE_FILE_CORRUPT"
    )
    _validate_paragraphs(paragraphs, character_count)
    return ValidatedKnowledgeUpload(filename, mime_type, sha256(data).hexdigest(), data)


def store_validated_upload(
    upload: ValidatedKnowledgeUpload, *, upload_dir: Path, document_id: str, version_id: str
) -> StoredKnowledgeUpload:
    _validate_storage_component(document_id)
    _validate_storage_component(version_id)
    suffix = Path(upload.original_filename).suffix.lower()
    try:
        root = upload_dir.resolve()
        parent = upload_dir
        if parent.is_symlink():
            raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.resolve().is_relative_to(root):
            raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")
        for component in (document_id, version_id):
            parent = parent / component
            if parent.is_symlink():
                raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")
            parent.mkdir(exist_ok=True)
            if parent.is_symlink() or not parent.resolve().is_relative_to(root):
                raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")

        target = parent / f"{upload.sha256}{suffix}"
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")
    except OSError as error:
        raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID") from error
    created = False
    try:
        with target.open("xb") as output:
            created = True
            output.write(upload.data)
    except OSError as error:
        if created and target.exists() and not target.is_symlink():
            target.unlink()
        raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID") from error
    return StoredKnowledgeUpload(
        original_filename=upload.original_filename,
        mime_type=upload.mime_type,
        sha256=upload.sha256,
        data=upload.data,
        storage_path=target,
    )


async def parse_and_chunk(
    stored: StoredKnowledgeUpload, *, token_count: Callable[[str], int], timeout_seconds: float
) -> list[ChunkDraft]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_parse_and_chunk_sync, stored, token_count), timeout_seconds
        )
    except TimeoutError as error:
        raise KnowledgeContentError("KNOWLEDGE_PARSE_TIMEOUT") from error


def _parse_and_chunk_sync(
    stored: StoredKnowledgeUpload, token_count: Callable[[str], int]
) -> list[ChunkDraft]:
    paragraphs, character_count = _source_paragraphs(
        stored.original_filename,
        stored.mime_type,
        stored.data,
        "KNOWLEDGE_PARSE_FAILED",
    )
    _validate_paragraphs(paragraphs, character_count)
    drafts: list[ChunkDraft] = []
    for paragraph in paragraphs:
        for canonical_text in _split_for_token_limit(paragraph.text, token_count):
            count = _count_tokens(canonical_text, token_count)
            chunk_hash = sha256(canonical_text.encode("utf-8")).hexdigest()
            chunk_index = len(drafts)
            drafts.append(
                ChunkDraft(
                    chunk_index=chunk_index,
                    chunk_id=stable_chunk_id(stored.sha256, chunk_index, chunk_hash),
                    chunk_hash=chunk_hash,
                    canonical_text=canonical_text,
                    chunk_metadata={
                        "heading_path": list(paragraph.heading_path),
                        "page_number": paragraph.page_number,
                        "paragraph_index": paragraph.paragraph_index,
                    },
                    token_count=count,
                )
            )
    if not drafts:
        raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")
    return drafts


def stable_chunk_id(version_sha256: str, chunk_index: int, chunk_hash: str) -> str:
    return str(uuid5(_CHUNK_NAMESPACE, f"{version_sha256}:{chunk_index}:{chunk_hash}"))


def _validate_client_filename(filename: str | None) -> Path:
    if not filename:
        raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")
    posix = PurePosixPath(filename)
    windows = PureWindowsPath(filename)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or posix.name != filename
        or windows.name != filename
    ):
        raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")
    return Path(filename)


def _validate_storage_component(component: str) -> None:
    if not component or any(
        path.is_absolute() or path.name != component
        for path in (PurePosixPath(component), PureWindowsPath(component))
    ):
        raise KnowledgeContentError("KNOWLEDGE_PATH_INVALID")


def _source_paragraphs(
    filename: str, mime_type: str, data: bytes, corrupt_code: str
) -> tuple[list[_Paragraph], int]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise KnowledgeContentError(corrupt_code) from error
        paragraphs, _, _, character_count = _text_paragraphs_with_state(text, None, (), 0)
        return paragraphs, character_count
    if suffix == ".pdf" and mime_type == _MIME_BY_SUFFIX[suffix]:
        return _pdf_paragraphs(data, corrupt_code)
    if suffix == ".docx" and mime_type == _MIME_BY_SUFFIX[suffix]:
        return _docx_paragraphs(data, corrupt_code)
    raise KnowledgeContentError(corrupt_code)


def _pdf_paragraphs(data: bytes, corrupt_code: str) -> tuple[list[_Paragraph], int]:
    if not data.startswith(b"%PDF-"):
        raise KnowledgeContentError(corrupt_code)
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
            raise KnowledgeContentError(corrupt_code)
        pages = [page.extract_text() or "" for page in reader.pages]
    except KnowledgeContentError:
        raise
    except Exception as error:
        raise KnowledgeContentError(corrupt_code) from error

    paragraphs: list[_Paragraph] = []
    heading_path: tuple[str, ...] = ()
    index = 0
    character_count = 0
    for page_number, text in enumerate(pages, start=1):
        page_paragraphs, heading_path, index, page_character_count = _text_paragraphs_with_state(
            text, page_number, heading_path, index
        )
        paragraphs.extend(page_paragraphs)
        character_count += page_character_count
    return paragraphs, character_count


def _docx_paragraphs(data: bytes, corrupt_code: str) -> tuple[list[_Paragraph], int]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > MAX_DOCX_ENTRIES
                or any(entry.flag_bits & 0x1 for entry in entries)
                or any(
                    entry.filename.replace("\\", "/").lower().startswith("word/embeddings/")
                    for entry in entries
                )
                or any(entry.file_size > MAX_DOCX_UNCOMPRESSED_BYTES for entry in entries)
                or sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES
            ):
                raise KnowledgeContentError(corrupt_code)
        document = Document(BytesIO(data))
    except KnowledgeContentError:
        raise
    except Exception as error:
        raise KnowledgeContentError(corrupt_code) from error

    paragraphs: list[_Paragraph] = []
    heading_path: list[str] = []
    index = 0
    character_count = 0
    for paragraph in document.paragraphs:
        text = _normalize(paragraph.text)
        if not text:
            continue
        character_count += len(text)
        heading = re.search(r"(\d+)$", paragraph.style.name or "")
        if paragraph.style.name.lower().startswith("heading") and heading:
            level = int(heading.group(1))
            heading_path = heading_path[: level - 1] + [text]
            continue
        paragraphs.append(_Paragraph(text, tuple(heading_path), None, index))
        index += 1
    return paragraphs, character_count


def _text_paragraphs_with_state(
    text: str,
    page_number: int | None,
    heading_path: tuple[str, ...],
    index: int,
) -> tuple[list[_Paragraph], tuple[str, ...], int, int]:
    paragraphs: list[_Paragraph] = []
    current: list[str] = []
    path = list(heading_path)
    character_count = 0

    def flush() -> None:
        nonlocal character_count, current, index
        normalized = _normalize(" ".join(current))
        if normalized:
            paragraphs.append(_Paragraph(normalized, tuple(path), page_number, index))
            character_count += len(normalized)
            index += 1
        current = []

    for line in [*text.splitlines(), ""]:
        normalized = _normalize(line)
        heading = _HEADING.match(normalized)
        if heading:
            flush()
            level = len(heading.group(1))
            title = _normalize(heading.group(2))
            character_count += len(title)
            path = path[: level - 1] + [title]
        elif normalized:
            current.append(normalized)
        else:
            flush()
    return paragraphs, tuple(path), index, character_count


def _validate_paragraphs(paragraphs: list[_Paragraph], character_count: int) -> None:
    if not paragraphs or character_count > MAX_NORMALIZED_CHARS:
        raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")


def _split_for_token_limit(text: str, token_count: Callable[[str], int]) -> list[str]:
    text = _normalize(text)
    if _count_tokens(text, token_count) <= MAX_CHUNK_TOKENS:
        return [text]
    sentences = [sentence for sentence in _SENTENCE.findall(text) if sentence.strip()]
    if len(sentences) <= 1:
        return _split_by_unicode(text, token_count)

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        prefix = _overlap_suffix(chunks[-1], token_count) if not current and chunks else ""
        candidate = _append_text(current or prefix, sentence)
        if _count_tokens(candidate, token_count) <= MAX_CHUNK_TOKENS:
            current = candidate
            continue
        if current:
            chunks.append(_normalize(current))
            prefix = _overlap_suffix(current, token_count)
        current = _append_text(prefix, sentence)
        if _count_tokens(current, token_count) > MAX_CHUNK_TOKENS:
            chunks.extend(_split_by_unicode(current, token_count))
            current = ""
    if current:
        chunks.append(_normalize(current))
    return chunks


def _split_by_unicode(text: str, token_count: Callable[[str], int]) -> list[str]:
    text = _normalize(text)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _largest_token_safe_end(text, start, token_count)
        chunk = _normalize(text[start:end])
        if not chunk:
            raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")
        chunks.append(chunk)
        if end == len(text):
            break
        overlap = _overlap_suffix(chunk, token_count)
        next_start = text.rfind(overlap, start, end)
        start = next_start if next_start > start else end
    return chunks


def _largest_token_safe_end(text: str, start: int, token_count: Callable[[str], int]) -> int:
    low, high = start + 1, len(text)
    best = start
    while low <= high:
        middle = (low + high) // 2
        if _count_tokens(_normalize(text[start:middle]), token_count) <= MAX_CHUNK_TOKENS:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best if best > start else start + 1


def _overlap_suffix(text: str, token_count: Callable[[str], int]) -> str:
    low, high = 0, len(text)
    best = len(text)
    while low <= high:
        middle = (low + high) // 2
        suffix = _normalize(text[middle:])
        if _count_tokens(suffix, token_count) <= CHUNK_OVERLAP_TOKENS:
            best = middle
            high = middle - 1
        else:
            low = middle + 1
    while best and not text[best - 1].isspace():
        candidate = best - 1
        if _count_tokens(_normalize(text[candidate:]), token_count) > CHUNK_OVERLAP_TOKENS:
            break
        best = candidate
    return _normalize(text[best:])


def _append_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right or left[-1].isspace() or right[0].isspace():
        return left + right
    return f"{left} {right}"


def _count_tokens(text: str, token_count: Callable[[str], int]) -> int:
    try:
        count = int(token_count(text))
    except Exception as error:
        raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED") from error
    if count <= 0:
        raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")
    return count


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
