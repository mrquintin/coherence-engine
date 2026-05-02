"""Browser-mode interview service + router tests (prompt 39).

Covers:

* ``start_browser_session`` mints a ``channel="browser"`` row.
* ``stitch_chunks`` produces a deterministic concatenation of the
  in-storage chunk blobs (compared via SHA-256 against an expected
  byte-stream concatenation).
* The router's ``:initiate`` endpoint refuses out-of-order ``seq``.
* The router's ``:complete`` endpoint verifies the bytes that were
  PUT to the signed URL and records SHA-256 + size.
* ``:finalize`` emits exactly one ``interview_session_completed``
  outbox event and is idempotent on a second call.

The ffmpeg stitcher is fakeable: tests inject a no-op binary that
just concatenates chunk bytes, so no real ffmpeg is required on the
test runner. The default code path on a developer machine still
exercises the real ffmpeg if it is installed, but CI does not depend
on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except BaseException as _exc:  # pragma: no cover - dependency missing
    pytest.skip(
        f"FastAPI unavailable in this interpreter: {_exc}",
        allow_module_level=True,
    )

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.routers.interview_browser import (
    router as interview_browser_router,
)
from coherence_engine.server.fund.services import object_storage, voice_intake
from coherence_engine.server.fund.services.storage_backends import (
    LocalFilesystemBackend,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_ffmpeg_path(tmp_path: Path) -> str:
    """Write a tiny shell shim that emulates ``ffmpeg -f concat`` semantics.

    The shim parses the concat list (``file '<path>'`` lines) and
    binary-concatenates each file into the output path. That is
    exactly what ``-c copy`` does for matching opus-in-webm chunks
    produced by a single MediaRecorder instance. The fake makes the
    test deterministic without depending on a system ffmpeg.
    """
    shim = tmp_path / "fake_ffmpeg.py"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, re\n"
        "argv = sys.argv[1:]\n"
        "list_path = argv[argv.index('-i') + 1]\n"
        "out_path = argv[-1]\n"
        "with open(list_path, 'r') as fh:\n"
        "    files = [re.match(r\"file '(.+)'\", line.strip()).group(1) "
        "for line in fh if line.strip()]\n"
        "with open(out_path, 'wb') as out:\n"
        "    for f in files:\n"
        "        with open(f, 'rb') as inp:\n"
        "            out.write(inp.read())\n"
    )
    shim.chmod(0o755)
    # Place a wrapper named ``ffmpeg`` on PATH that execs the shim.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "ffmpeg"
    wrapper.write_text(
        f"#!/usr/bin/env bash\nexec {shutil.which('python3') or 'python3'} "
        f"{shim} \"$@\"\n"
    )
    wrapper.chmod(0o755)
    return str(wrapper)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    backend = LocalFilesystemBackend(root=str(tmp_path / "storage"), bucket="default")
    object_storage.set_object_storage(backend)

    yield tmp_path

    object_storage.reset_object_storage()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _seed_application() -> str:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="f_browser_test",
            full_name="Test Founder",
            email="t@example.com",
            company_name="Acme",
            country="US",
        )
        app = models.Application(
            id="app_browser_test",
            founder_id=founder.id,
            one_liner="Acme builds widgets.",
            requested_check_usd=250000,
            use_of_funds_summary="hire",
            preferred_channel="browser",
            domain_primary="market_economics",
        )
        db.add(founder)
        db.add(app)
        db.commit()
        return app.id
    finally:
        db.close()


def _seed_browser_session(application_id: str) -> models.InterviewSession:
    db = SessionLocal()
    try:
        s = voice_intake.start_browser_session(db, application_id=application_id)
        db.commit()
        db.refresh(s)
        return s
    finally:
        db.close()


def _put_chunks_directly(session_id: str, application_id: str, payloads: list[bytes]) -> None:
    """Persist completed chunk rows + payload blobs in storage."""
    db = SessionLocal()
    try:
        from datetime import datetime, timezone

        for seq, data in enumerate(payloads):
            key = f"interviews/{session_id}/chunk_{seq:05d}.webm"
            result = object_storage.put(key, data, content_type="audio/webm")
            row = models.InterviewChunk(
                id=f"chk_seed_{seq:05d}",
                session_id=session_id,
                application_id=application_id,
                seq=seq,
                chunk_uri=result.uri,
                chunk_sha256=result.sha256,
                size_bytes=len(data),
                content_type="audio/webm",
                status="completed",
                completed_at=datetime.now(tz=timezone.utc),
            )
            db.add(row)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


def test_start_browser_session_creates_browser_channel_row():
    app_id = _seed_application()
    db = SessionLocal()
    try:
        session = voice_intake.start_browser_session(db, application_id=app_id)
        db.commit()
        assert session.channel == "browser"
        assert session.status == "active"
        assert session.id.startswith("ivw_browser_")
        assert session.application_id == app_id
    finally:
        db.close()


def test_stitch_chunks_produces_deterministic_concatenation(tmp_path):
    app_id = _seed_application()
    session = _seed_browser_session(app_id)
    payloads = [b"opus-frame-A" * 10, b"opus-frame-B" * 10, b"opus-frame-C" * 10]
    _put_chunks_directly(session.id, app_id, payloads)

    fake_ffmpeg = _fake_ffmpeg_path(tmp_path)
    db = SessionLocal()
    try:
        s = db.query(models.InterviewSession).filter_by(id=session.id).one()
        full_uri, full_sha, full_size = voice_intake.stitch_chunks(
            db, session=s, ffmpeg_binary=fake_ffmpeg
        )
        db.commit()
    finally:
        db.close()

    # Hash equals the hash of the byte-stream concatenation —
    # this is the determinism guarantee callers rely on.
    expected = hashlib.sha256(b"".join(payloads)).hexdigest()
    assert full_sha == expected
    assert full_size == sum(len(p) for p in payloads)
    assert object_storage.get(full_uri) == b"".join(payloads)


def test_stitch_chunks_rejects_seq_gaps(tmp_path):
    app_id = _seed_application()
    session = _seed_browser_session(app_id)

    # Insert chunk seq=0 and seq=2 (skip seq=1)
    db = SessionLocal()
    try:
        from datetime import datetime, timezone

        for seq in (0, 2):
            data = f"chunk-{seq}".encode("utf-8")
            key = f"interviews/{session.id}/chunk_{seq:05d}.webm"
            result = object_storage.put(key, data, content_type="audio/webm")
            db.add(
                models.InterviewChunk(
                    id=f"chk_gap_{seq}",
                    session_id=session.id,
                    application_id=app_id,
                    seq=seq,
                    chunk_uri=result.uri,
                    chunk_sha256=result.sha256,
                    size_bytes=len(data),
                    content_type="audio/webm",
                    status="completed",
                    completed_at=datetime.now(tz=timezone.utc),
                )
            )
        db.commit()
    finally:
        db.close()

    fake_ffmpeg = _fake_ffmpeg_path(tmp_path)
    db = SessionLocal()
    try:
        s = db.query(models.InterviewSession).filter_by(id=session.id).one()
        with pytest.raises(voice_intake.VoiceIntakeError) as excinfo:
            voice_intake.stitch_chunks(db, session=s, ffmpeg_binary=fake_ffmpeg)
        assert "seq_gap" in str(excinfo.value)
    finally:
        db.close()


def test_finalize_browser_session_emits_event_and_is_idempotent(tmp_path):
    app_id = _seed_application()
    session = _seed_browser_session(app_id)
    payloads = [b"chunkA", b"chunkB"]
    _put_chunks_directly(session.id, app_id, payloads)

    fake_ffmpeg = _fake_ffmpeg_path(tmp_path)
    db = SessionLocal()
    try:
        s = db.query(models.InterviewSession).filter_by(id=session.id).one()
        result = voice_intake.finalize_browser_session(
            db, session=s, ffmpeg_binary=fake_ffmpeg
        )
        db.commit()
        assert result is not None
        assert result["chunk_count"] == 2
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["channel"] == "browser"
        assert payload["session_id"] == session.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        s = db.query(models.InterviewSession).filter_by(id=session.id).one()
        again = voice_intake.finalize_browser_session(
            db, session=s, ffmpeg_binary=fake_ffmpeg
        )
        db.commit()
        assert again is None
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .all()
        )
        assert len(events) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(interview_browser_router, prefix="/api/v1")
    return TestClient(app)


def test_initiate_chunk_rejects_out_of_order_seq(client):
    app_id = _seed_application()
    session = _seed_browser_session(app_id)

    # seq=1 with no prior seq=0 → 409 SEQ_OUT_OF_ORDER
    res = client.post(
        f"/api/v1/interviews/{session.id}/chunks:initiate",
        json={"seq": 1, "size_bytes": 1024},
    )
    assert res.status_code == 409
    body = res.json()
    assert body["error"]["code"] == "SEQ_OUT_OF_ORDER"


def test_initiate_then_complete_chunk_records_metadata(client):
    app_id = _seed_application()
    session = _seed_browser_session(app_id)

    res = client.post(
        f"/api/v1/interviews/{session.id}/chunks:initiate",
        json={"seq": 0, "size_bytes": 1024},
    )
    assert res.status_code == 201, res.text
    init_data = res.json()["data"]
    chunk_id = init_data["chunk_id"]
    uri = init_data["uri"]

    # Simulate the browser PUTting bytes directly to the signed URL
    # by writing the same bytes through the storage backend.
    payload = b"opus-bytes-for-chunk-zero" * 4
    _, _, key = object_storage.parse_uri(uri)
    object_storage.put(key, payload, content_type="audio/webm")

    res = client.post(
        f"/api/v1/interviews/{session.id}/chunks:complete",
        json={"chunk_id": chunk_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["status"] == "completed"
    assert body["size_bytes"] == len(payload)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()


def test_finalize_router_emits_event_and_is_idempotent(client, tmp_path):
    app_id = _seed_application()
    session = _seed_browser_session(app_id)

    # Seed two completed chunks via the storage backend + DB rows.
    payloads = [b"router-A" * 8, b"router-B" * 8]
    _put_chunks_directly(session.id, app_id, payloads)

    # Inject the fake ffmpeg via FFMPEG_BINARY env override.
    os.environ["FFMPEG_BINARY"] = _fake_ffmpeg_path(tmp_path)
    try:
        res = client.post(f"/api/v1/interviews/{session.id}:finalize")
        assert res.status_code == 200, res.text
        body = res.json()["data"]
        assert body["status"] == "completed"
        assert body["chunk_count"] == 2
        assert body["full_uri"]
        assert body["idempotent"] is False

        # Idempotent re-call.
        res2 = client.post(f"/api/v1/interviews/{session.id}:finalize")
        assert res2.status_code == 200
        body2 = res2.json()["data"]
        assert body2["idempotent"] is True
        assert body2["status"] == "completed"
    finally:
        os.environ.pop("FFMPEG_BINARY", None)

    db = SessionLocal()
    try:
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .all()
        )
        assert len(events) == 1
    finally:
        db.close()
