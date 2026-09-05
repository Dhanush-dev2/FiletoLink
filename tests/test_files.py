"""Tests for file receiving and metadata extraction."""
import os
import tempfile

import pytest

from app.bot.handlers import format_size, handle_update
from app.bot.models import Document, Message, Update, User
from app.bot.mock import local_file_document, send_local_file

USER = User(id=42, username="alice", first_name="Alice")

# 2023-11-14 22:13:20 UTC
FIXED_DATE = 1_700_000_000


def make_file_update(
    document: Document,
    text: str | None = None,
    date: int | None = FIXED_DATE,
    user: User | None = USER,
) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=7,
            text=text,
            user=user,
            document=document,
            date=date,
        ),
    )


def test_file_received_displays_full_metadata():
    doc = Document(
        file_id="FILE123",
        file_name="example.pdf",
        file_size=4_404_019,  # 4.2 MiB
        mime_type="application/pdf",
    )
    reply = handle_update(make_file_update(doc))
    assert "File received" in reply
    assert "example.pdf" in reply
    assert "4.2 MB" in reply
    assert "Type: application/pdf" in reply
    assert "File ID: FILE123" in reply
    assert "Message ID: 7" in reply
    assert "User ID: 42" in reply
    assert "2023-11-14 22:13:20 UTC" in reply


def test_file_without_mime_type_omits_type_line():
    doc = Document(file_id="FILE1", file_name="data.bin", file_size=100)
    reply = handle_update(make_file_update(doc))
    assert "Type:" not in reply
    assert "File received" in reply


def test_file_without_name_uses_placeholder():
    doc = Document(file_id="FILE1", file_size=100)
    reply = handle_update(make_file_update(doc))
    assert "unnamed" in reply


def test_file_without_size_shows_unknown():
    doc = Document(file_id="FILE1", file_name="a.bin")
    reply = handle_update(make_file_update(doc))
    assert "unknown" in reply


def test_file_without_timestamp_omits_received_line():
    doc = Document(file_id="FILE1", file_name="a.bin", file_size=1)
    reply = handle_update(make_file_update(doc, date=None))
    assert "Received:" not in reply


def test_caption_with_document_prioritizes_file():
    doc = Document(file_id="FILE1", file_name="photo.png", file_size=2048)
    reply = handle_update(make_file_update(doc, text="look at this"))
    assert "File received" in reply
    assert "look at this" not in reply


def test_format_size():
    assert format_size(None) == "unknown"
    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(4_404_019) == "4.2 MB"
    assert format_size(1_073_741_824) == "1.0 GB"


def test_local_file_document_extracts_metadata():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp.write(b"hello world\n")
        path = tmp.name
    try:
        doc = local_file_document(path)
        assert doc.file_name == os.path.basename(path)
        assert doc.file_size == 12
        assert doc.mime_type == "text/plain"
        assert doc.file_id.startswith("BQAD")
        assert len(doc.file_id) > 10
    finally:
        os.unlink(path)


def test_send_local_file_acknowledges():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp.write(b"hello world\n")
        path = tmp.name
    try:
        reply = send_local_file(path, user=USER)
        assert "File received" in reply
        assert os.path.basename(path) in reply
        assert "12 B" in reply
        assert "text/plain" in reply
        assert "User ID: 42" in reply
    finally:
        os.unlink(path)


def test_send_local_file_missing_path_raises():
    with pytest.raises(FileNotFoundError):
        send_local_file("definitely/not/here.txt")