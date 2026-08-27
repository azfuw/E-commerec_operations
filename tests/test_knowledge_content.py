import asyncio
from io import BytesIO
from pathlib import Path
import struct
import time
import zipfile

from docx import Document
from fastapi import UploadFile
from pypdf import PdfWriter
import pytest
from starlette.datastructures import Headers

from backend.knowledge_content import (
    KnowledgeContentError,
    StoredKnowledgeUpload,
    parse_and_chunk,
    read_and_validate_upload,
    store_validated_upload,
)


MIB = 1024 * 1024
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _upload(data: bytes, filename: str, content_type: str) -> UploadFile:
    return UploadFile(BytesIO(data), filename=filename, headers=Headers({"content-type": content_type}))


async def _assert_code(awaitable, code: str) -> None:
    with pytest.raises(KnowledgeContentError) as error:
        await awaitable
    assert error.value.code == code


def _pdf(*, encrypted: bool = False, page_count: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("test-password")
    data = BytesIO()
    writer.write(data)
    return data.getvalue()


def _docx(text: str = "") -> bytes:
    document = Document()
    if text:
        document.add_paragraph(text)
    data = BytesIO()
    document.save(data)
    return data.getvalue()


def _docx_with_embedded_archive() -> bytes:
    data = BytesIO(_docx("规则"))
    with zipfile.ZipFile(data, "a") as archive:
        archive.writestr("word/embeddings/archive.zip", b"inert")
    return data.getvalue()


def _zip_with_claimed_sizes(sizes: list[int]) -> bytes:
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for index in range(len(sizes)):
            archive.writestr(f"word/entry-{index}.xml", b"x")
    raw = bytearray(data.getvalue())
    position = 0
    for size in sizes:
        position = raw.index(b"PK\x01\x02", position)
        struct.pack_into("<I", raw, position + 24, size)
        position += 4
    return bytes(raw)


async def test_accepts_strict_utf8_text_and_markdown() -> None:
    text = await read_and_validate_upload(_upload("规则".encode(), "rules.txt", "text/plain"))
    markdown = await read_and_validate_upload(
        _upload("# 标题\n\n规则".encode(), "rules.md", "text/markdown")
    )

    assert text.mime_type == "text/plain"
    assert markdown.mime_type == "text/markdown"
    assert text.sha256 != markdown.sha256


@pytest.mark.parametrize(
    ("data", "filename", "content_type", "code"),
    [
        (b"", "rules.txt", "text/plain", "KNOWLEDGE_FILE_EMPTY"),
        (b"\xff", "rules.txt", "text/plain", "KNOWLEDGE_FILE_CORRUPT"),
        (b"rules", "rules.exe", "text/plain", "KNOWLEDGE_FILE_TYPE_INVALID"),
        (b"rules", "rules.txt", "text/markdown", "KNOWLEDGE_MIME_MISMATCH"),
        (b"rules", "../rules.txt", "text/plain", "KNOWLEDGE_PATH_INVALID"),
        (b"rules", r"C:\\rules.txt", "text/plain", "KNOWLEDGE_PATH_INVALID"),
    ],
)
async def test_rejects_invalid_text_upload_boundaries(data, filename, content_type, code) -> None:
    await _assert_code(read_and_validate_upload(_upload(data, filename, content_type)), code)


async def test_rejects_upload_larger_than_twenty_mib() -> None:
    await _assert_code(
        read_and_validate_upload(_upload(b"x" * (20 * MIB + 1), "rules.txt", "text/plain")),
        "KNOWLEDGE_FILE_TOO_LARGE",
    )


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"%PDF-not-a-pdf", "KNOWLEDGE_FILE_CORRUPT"),
        (_pdf(encrypted=True), "KNOWLEDGE_FILE_CORRUPT"),
        (_pdf(page_count=201), "KNOWLEDGE_FILE_CORRUPT"),
        (_pdf(), "KNOWLEDGE_PARSE_FAILED"),
    ],
    ids=["malformed", "encrypted", "page-limit", "empty-text"],
)
async def test_rejects_corrupt_or_unusable_pdfs(data, code) -> None:
    await _assert_code(
        read_and_validate_upload(_upload(data, "rules.pdf", "application/pdf")),
        code,
    )


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"PK-not-a-docx", "KNOWLEDGE_FILE_CORRUPT"),
        (_zip_with_claimed_sizes([100 * MIB + 1]), "KNOWLEDGE_FILE_CORRUPT"),
        (_zip_with_claimed_sizes([60 * MIB, 60 * MIB]), "KNOWLEDGE_FILE_CORRUPT"),
        (_docx(), "KNOWLEDGE_PARSE_FAILED"),
    ],
    ids=["malformed-zip", "entry-limit", "total-limit", "empty-text"],
)
async def test_rejects_corrupt_or_unusable_docx_archives(data, code) -> None:
    await _assert_code(read_and_validate_upload(_upload(data, "rules.docx", DOCX_MIME)), code)


async def test_rejects_docx_with_more_than_one_thousand_entries() -> None:
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for index in range(1001):
            archive.writestr(f"word/entry-{index}.xml", b"x")

    await _assert_code(
        read_and_validate_upload(_upload(data.getvalue(), "rules.docx", DOCX_MIME)),
        "KNOWLEDGE_FILE_CORRUPT",
    )


async def test_rejects_docx_with_embedded_archive() -> None:
    await _assert_code(
        read_and_validate_upload(
            _upload(_docx_with_embedded_archive(), "rules.docx", DOCX_MIME)
        ),
        "KNOWLEDGE_FILE_CORRUPT",
    )


async def test_store_uses_a_generated_filename_inside_the_controlled_directory(tmp_path) -> None:
    upload = await read_and_validate_upload(_upload("规则".encode(), "client-name.md", "text/markdown"))
    stored = store_validated_upload(
        upload,
        upload_dir=tmp_path / "uploads",
        document_id="document-1",
        version_id="version-1",
    )

    assert stored.storage_path.parent == tmp_path / "uploads" / "document-1" / "version-1"
    assert stored.storage_path.name != upload.original_filename
    assert stored.storage_path.read_bytes() == upload.data


async def test_store_creates_a_missing_multi_level_upload_root(tmp_path) -> None:
    upload = await read_and_validate_upload(_upload("规则".encode(), "rules.md", "text/markdown"))
    root = tmp_path / "missing-a" / "missing-b" / "knowledge"

    stored = store_validated_upload(
        upload,
        upload_dir=root,
        document_id="document-1",
        version_id="version-1",
    )

    assert stored.storage_path.is_relative_to(root)
    assert stored.storage_path.read_bytes() == upload.data


async def test_store_maps_upload_root_creation_failure_to_a_safe_error(tmp_path) -> None:
    upload = await read_and_validate_upload(_upload("规则".encode(), "rules.md", "text/markdown"))
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"not-a-directory")

    with pytest.raises(KnowledgeContentError) as error:
        store_validated_upload(
            upload,
            upload_dir=blocked / "knowledge",
            document_id="document-1",
            version_id="version-1",
        )

    assert error.value.code == "KNOWLEDGE_PATH_INVALID"


async def test_store_preserves_an_existing_generated_file(tmp_path) -> None:
    upload = await read_and_validate_upload(_upload("规则".encode(), "rules.md", "text/markdown"))
    root = tmp_path / "uploads"
    target = root / "document-1" / "version-1" / f"{upload.sha256}.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    with pytest.raises(KnowledgeContentError) as error:
        store_validated_upload(
            upload,
            upload_dir=root,
            document_id="document-1",
            version_id="version-1",
        )

    assert error.value.code == "KNOWLEDGE_PATH_INVALID"
    assert target.read_bytes() == b"existing"


async def test_store_rejects_a_generated_parent_symlink_without_writing_outside(
    monkeypatch, tmp_path
) -> None:
    upload = await read_and_validate_upload(_upload("规则".encode(), "rules.md", "text/markdown"))
    root = tmp_path / "uploads"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "document-1").mkdir(parents=True)
    generated_parent = root / "document-1" / "version-1"
    try:
        generated_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == generated_parent or original_is_symlink(path),
        )

    with pytest.raises(KnowledgeContentError) as error:
        store_validated_upload(
            upload,
            upload_dir=root,
            document_id="document-1",
            version_id="version-1",
        )

    assert error.value.code == "KNOWLEDGE_PATH_INVALID"
    assert not list(outside.iterdir())


async def test_parse_timeout_returns_a_safe_error_without_drafts(monkeypatch, tmp_path) -> None:
    stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="a" * 64,
        data=b"rules",
        storage_path=tmp_path / "rules.txt",
    )

    def block(*_args):
        time.sleep(0.05)
        return []

    monkeypatch.setattr("backend.knowledge_content._parse_and_chunk_sync", block)
    await _assert_code(
        parse_and_chunk(stored, token_count=lambda text: len(text.split()), timeout_seconds=0.001),
        "KNOWLEDGE_PARSE_TIMEOUT",
    )


async def test_parse_rejects_normalized_text_over_one_million_characters(tmp_path) -> None:
    stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="a" * 64,
        data=b"x" * 1_000_001,
        storage_path=tmp_path / "rules.txt",
    )

    await _assert_code(
        parse_and_chunk(stored, token_count=len, timeout_seconds=1),
        "KNOWLEDGE_PARSE_FAILED",
    )


async def test_parse_rejects_normalized_heading_over_one_million_characters(tmp_path) -> None:
    stored = StoredKnowledgeUpload(
        original_filename="rules.md",
        mime_type="text/markdown",
        sha256="d" * 64,
        data=("# " + "x" * 1_000_001 + "\n\n规则").encode(),
        storage_path=tmp_path / "rules.md",
    )

    await _assert_code(
        parse_and_chunk(stored, token_count=len, timeout_seconds=1),
        "KNOWLEDGE_PARSE_FAILED",
    )


async def test_chunks_keep_heading_and_paragraph_positions_deterministically(tmp_path) -> None:
    stored = StoredKnowledgeUpload(
        original_filename="rules.md",
        mime_type="text/markdown",
        sha256="a" * 64,
        data="# 标题\n\n相同段落\n\n相同段落".encode(),
        storage_path=tmp_path / "rules.md",
    )

    first = await parse_and_chunk(
        stored,
        token_count=lambda text: len(text.split()),
        timeout_seconds=1,
    )
    second = await parse_and_chunk(
        stored,
        token_count=lambda text: len(text.split()),
        timeout_seconds=1,
    )

    assert first == second
    assert [draft.chunk_index for draft in first] == [0, 1]
    assert [draft.chunk_metadata["heading_path"] for draft in first] == [["标题"], ["标题"]]
    assert [draft.chunk_metadata["paragraph_index"] for draft in first] == [0, 1]
    assert first[0].chunk_hash == first[1].chunk_hash
    assert first[0].chunk_id != first[1].chunk_id


async def test_oversized_paragraphs_use_token_overlap_and_unicode_fallback(tmp_path) -> None:
    words = " ".join(f"word-{index}" for index in range(513))
    word_stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="b" * 64,
        data=words.encode(),
        storage_path=tmp_path / "words.txt",
    )
    word_chunks = await parse_and_chunk(
        word_stored,
        token_count=lambda text: len(text.split()),
        timeout_seconds=1,
    )
    unicode_stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="c" * 64,
        data=("甲" * 513).encode(),
        storage_path=tmp_path / "unicode.txt",
    )
    unicode_chunks = await parse_and_chunk(unicode_stored, token_count=len, timeout_seconds=1)

    assert all(draft.token_count <= 512 for draft in word_chunks + unicode_chunks)
    assert word_chunks[0].canonical_text.split()[-64:] == word_chunks[1].canonical_text.split()[:64]
    assert unicode_chunks[0].canonical_text[-64:] == unicode_chunks[1].canonical_text[:64]


async def test_oversized_sentence_keeps_overlap_with_the_preceding_short_sentence(tmp_path) -> None:
    short_sentence = " ".join(f"short-{index}" for index in range(100)) + "."
    long_sentence = " ".join(f"long-{index}" for index in range(600)) + "."
    stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="e" * 64,
        data=f"{short_sentence} {long_sentence}".encode(),
        storage_path=tmp_path / "rules.txt",
    )

    chunks = await parse_and_chunk(
        stored,
        token_count=lambda text: len(text.split()),
        timeout_seconds=1,
    )

    assert all(chunk.token_count <= 512 for chunk in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        assert previous.canonical_text.split()[-64:] == following.canonical_text.split()[:64]


async def test_consecutive_oversized_sentences_keep_overlap_across_every_chunk(tmp_path) -> None:
    first_sentence = " ".join(f"first-{index}" for index in range(600)) + "."
    second_sentence = " ".join(f"second-{index}" for index in range(600)) + "."
    stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="f" * 64,
        data=f"{first_sentence} {second_sentence}".encode(),
        storage_path=tmp_path / "rules.txt",
    )

    chunks = await parse_and_chunk(
        stored,
        token_count=lambda text: len(text.split()),
        timeout_seconds=1,
    )

    assert len(chunks) > 1
    assert all(chunk.token_count <= 512 for chunk in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        assert previous.canonical_text.split()[-64:] == following.canonical_text.split()[:64]
        assert following.canonical_text.split() != previous.canonical_text.split()[-64:]


async def test_oversized_sentence_keeps_overlap_with_a_following_short_sentence(tmp_path) -> None:
    long_sentence = " ".join(f"long-{index}" for index in range(600)) + "."
    short_sentence = " ".join(f"short-{index}" for index in range(100)) + "."
    stored = StoredKnowledgeUpload(
        original_filename="rules.txt",
        mime_type="text/plain",
        sha256="0" * 64,
        data=f"{long_sentence} {short_sentence}".encode(),
        storage_path=tmp_path / "rules.txt",
    )

    chunks = await parse_and_chunk(
        stored,
        token_count=lambda text: len(text.split()),
        timeout_seconds=1,
    )

    assert all(chunk.token_count <= 512 for chunk in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        assert previous.canonical_text.split()[-64:] == following.canonical_text.split()[:64]
