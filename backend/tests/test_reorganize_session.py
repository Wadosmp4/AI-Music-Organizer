"""Tests for ReorganizeSessionRepository (U1, KTD2)."""

from app.models.library import LibraryItem
from app.models.reorganize_session import ReorganizeSession
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository


def _make_user(session) -> User:
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_get_open_for_user_returns_none_when_no_sessions_exist(session):
    user = _make_user(session)
    repo = ReorganizeSessionRepository(session)
    assert repo.get_open_for_user(user.id) is None


def test_get_open_for_user_returns_session_with_non_terminal_snapshot_member(session):
    user = _make_user(session)
    library_item = LibraryItem(
        user_id=user.id, video_id="v1", title="Song", artist="Artist"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    reorganize_session = ReorganizeSession(
        user_id=user.id,
        video_id_snapshot=["v1"],
        clustering_status="done",
        apply_status="idle",
    )
    session.add(reorganize_session)
    session.commit()
    session.refresh(reorganize_session)

    # A pending, session-tagged item against the snapshot's one song --
    # matching/apply for it is not yet terminal.
    item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_item.id,
        status="pending",
        reorganize_session_id=reorganize_session.id,
    )
    session.add(item)
    session.commit()

    repo = ReorganizeSessionRepository(session)
    found = repo.get_open_for_user(user.id)
    assert found is not None
    assert found.id == reorganize_session.id


def test_get_open_for_user_returns_none_when_every_snapshot_member_is_terminal(session):
    user = _make_user(session)
    library_item = LibraryItem(
        user_id=user.id, video_id="v1", title="Song", artist="Artist"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    reorganize_session = ReorganizeSession(
        user_id=user.id,
        video_id_snapshot=["v1"],
        clustering_status="done",
        apply_status="idle",
    )
    session.add(reorganize_session)
    session.commit()
    session.refresh(reorganize_session)

    item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_item.id,
        status="approved",
        reorganize_session_id=reorganize_session.id,
    )
    session.add(item)
    session.commit()

    repo = ReorganizeSessionRepository(session)
    assert repo.get_open_for_user(user.id) is None


def test_get_open_for_user_returns_session_still_clustering(session):
    user = _make_user(session)
    reorganize_session = ReorganizeSession(
        user_id=user.id,
        video_id_snapshot=["v1", "v2"],
        clustering_status="in_progress",
        apply_status="idle",
    )
    session.add(reorganize_session)
    session.commit()
    session.refresh(reorganize_session)

    repo = ReorganizeSessionRepository(session)
    found = repo.get_open_for_user(user.id)
    assert found is not None
    assert found.id == reorganize_session.id
