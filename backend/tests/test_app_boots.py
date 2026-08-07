import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.main import app
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.review_queue_repository import ReviewQueueRepository


def test_health_endpoint_responds():
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_migrations_apply_cleanly_from_empty_database(tmp_path):
    db_path = tmp_path / "migration-check.db"
    backend_dir = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert db_path.exists()


def test_reorganize_session_migration_downgrades_and_reupgrades_cleanly(tmp_path):
    """U1: the CHECK-constraint change (approved_pending_apply) requires
    SQLite batch mode (op.batch_alter_table) -- verifies the whole chain
    still downgrades (dropping the new table/column and reverting the
    constraint) and re-upgrades cleanly."""
    db_path = tmp_path / "migration-roundtrip.db"
    backend_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}

    upgrade_result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert upgrade_result.returncode == 0, upgrade_result.stderr

    downgrade_result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "-1"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert downgrade_result.returncode == 0, downgrade_result.stderr

    reupgrade_result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert reupgrade_result.returncode == 0, reupgrade_result.stderr


def test_domain_tables_reject_null_user_id(session):
    with pytest.raises(IntegrityError):
        session.add(LibraryItem(video_id="abc123", title="Song", artist="Artist"))
        session.commit()


def test_foreign_key_violation_is_rejected(session):
    with pytest.raises(IntegrityError):
        session.add(
            LibraryItem(user_id=999, video_id="abc123", title="Song", artist="Artist")
        )
        session.commit()


def test_invalid_review_queue_status_rejected_by_database(session):
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)

    library_item = LibraryItem(
        user_id=user.id, video_id="abc123", title="Song", artist="Artist"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    with pytest.raises(IntegrityError):
        session.add(
            ReviewQueueItem(
                user_id=user.id,
                library_item_id=library_item.id,
                status="not-a-real-status",
            )
        )
        session.commit()


def test_approved_pending_apply_status_accepted_by_database(session):
    """U1/KTD3: approved_pending_apply is a real, session-deferred decided
    state -- distinct from `not-a-real-status` above -- and must be a legal
    value under the CHECK constraint."""
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)

    library_item = LibraryItem(
        user_id=user.id, video_id="def456", title="Song", artist="Artist"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_item.id,
        status="approved_pending_apply",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    assert item.status == "approved_pending_apply"


def test_repository_usable_directly_without_service_or_api_layer(session):
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)

    library_item = LibraryItem(
        user_id=user.id, video_id="xyz789", title="Song", artist="Artist"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    repo = ReviewQueueRepository(session)
    created = repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=library_item.id)
    )

    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.version == 1

    updated = repo.update_status(created.id, expected_version=1, new_status="approved")
    assert updated.status == "approved"
    assert updated.version == 2
