"""U7/R15: correction-feedback loop.

Covers:
1. A logged correction shifts a subsequently-classified similar song toward
   the playlist the user actually moved the earlier song to.
2. Logging a correction never touches any other already-pending
   ReviewQueueItem row (KTD12: corrections shape only *future*
   classify_track calls, never a retroactive re-evaluation).
3. `correction_log_entry` is append-only at the DB layer (KTD20): a raw
   UPDATE or DELETE against an existing row is rejected by the SQLite
   triggers from migration 1, not merely by repository convention.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import SQLModel, create_engine

from app.models.correction_log import CorrectionLogEntry
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.bpm_lookup import BpmLookupResult, BpmLookupService
from app.services.classification import CandidatePlaylist, ClassificationService
from app.services.genre_lookup import GenreLookupService


@pytest.fixture()
def engine(tmp_path):
    """Overrides conftest's bare `engine` fixture for this module only.

    conftest's version builds the schema via `SQLModel.metadata.create_all`,
    which reflects Python model definitions but doesn't carry hand-written
    migration SQL — in particular the `correction_log_entry_no_update` /
    `correction_log_entry_no_delete` triggers from
    `alembic/versions/1_initial_schema.py` that enforce KTD20's
    append-only guarantee at the DB layer. Recreating those two triggers
    here (verbatim from that migration) is what lets
    `test_correction_log_entry_is_append_only_at_the_db_layer` prove real
    DB-level rejection instead of only a repository-convention gap. Every
    other test in this module still gets a plain, real SQLite-file-backed
    engine via `session` (from conftest), unaffected by this except for the
    two extra triggers, which only fire on UPDATE/DELETE against
    `correction_log_entry` — a statement none of the other tests here issue.
    """
    db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
    test_engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(test_engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    from app import models  # noqa: F401

    SQLModel.metadata.create_all(test_engine)

    with test_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER correction_log_entry_no_update
                BEFORE UPDATE ON correction_log_entry
                BEGIN
                    SELECT RAISE(ABORT, 'correction_log_entry is append-only: updates are not allowed');
                END;
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER correction_log_entry_no_delete
                BEFORE DELETE ON correction_log_entry
                BEGIN
                    SELECT RAISE(ABORT, 'correction_log_entry is append-only: deletes are not allowed');
                END;
                """
            )
        )

    yield test_engine


def _service(genre=None, correction_log_repo=None):
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = genre
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    bpm_lookup.openrouter_api_key = None
    return ClassificationService(
        genre_lookup, bpm_lookup, correction_log_repo=correction_log_repo
    )


def _seed_correction(session, *, artist_bucket_key: str, genre=None) -> tuple[User, Playlist, Playlist]:
    """Builds the full FK chain a real correction requires (user, both
    playlists, the original library item + its review queue item), then logs
    the correction: original_playlist -> corrected_playlist for that artist.
    """
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)

    original_playlist = Playlist(user_id=user.id, name="Playlist 1", rule=None, description=None)
    corrected_playlist = Playlist(user_id=user.id, name="Playlist 2", rule=None, description=None)
    session.add(original_playlist)
    session.add(corrected_playlist)
    session.commit()
    session.refresh(original_playlist)
    session.refresh(corrected_playlist)

    library_item = LibraryItem(
        user_id=user.id, video_id="song-a", title="Song A", artist="Artist X"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    queue_item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_item.id,
        playlist_id=original_playlist.id,
        status="moved",
        confidence=0.5,
        explanation={"signal": "artist_similarity", "detail": "seed"},
    )
    session.add(queue_item)
    session.commit()
    session.refresh(queue_item)

    correction_repo = CorrectionLogRepository(session)
    correction_repo.create(
        CorrectionLogEntry(
            user_id=user.id,
            review_queue_item_id=queue_item.id,
            original_playlist_id=original_playlist.id,
            corrected_playlist_id=corrected_playlist.id,
            context={"artist_bucket_key": artist_bucket_key, "artist": "Artist X", "genre": genre},
        )
    )

    return user, original_playlist, corrected_playlist


def test_correction_shifts_a_similar_future_song_to_the_corrected_playlist(session):
    # Song A by "Artist X" was suggested for Playlist 1 but the user moved it
    # to Playlist 2 — logged as a real correction_log_entry row.
    user, original_playlist, corrected_playlist = _seed_correction(
        session, artist_bucket_key="artistx"
    )

    # A new song B, also by "Artist X", with neither playlist having any
    # existing-song history for this artist (artist_counts all zero) — so
    # without the correction, artist-similarity can't clear MIN_MATCH_SCORE
    # for either playlist.
    candidates = [
        CandidatePlaylist(
            id=original_playlist.id, name="Playlist 1", rule=None, description=None, artist_counts={}
        ),
        CandidatePlaylist(
            id=corrected_playlist.id, name="Playlist 2", rule=None, description=None, artist_counts={}
        ),
    ]
    track_b = {"title": "Song B", "artists": [{"name": "Artist X"}]}

    # Baseline: no correction feedback wired in at all — byte-for-byte the
    # pre-U7 behavior. Neither playlist has existing-song history, so nothing
    # clears MIN_MATCH_SCORE and there's no description/LLM signal either.
    baseline_service = _service()
    baseline_result = baseline_service.classify_track(track_b, candidates)
    assert baseline_result.playlist_id is None
    assert baseline_result.explanation.signal == "none"

    # With the correction-log repo wired in and scoped to this user, the
    # recent correction for the same artist bucket now favors Playlist 2.
    feedback_service = _service(correction_log_repo=CorrectionLogRepository(session))
    feedback_result = feedback_service.classify_track(track_b, candidates, user_id=user.id)

    assert feedback_result.playlist_id == corrected_playlist.id
    assert feedback_result.explanation.signal == "correction_feedback"


def test_correction_feedback_is_scoped_by_genre_too(session):
    user, original_playlist, corrected_playlist = _seed_correction(
        session, artist_bucket_key="someotherartist", genre="synthpop"
    )

    # Different artist bucket, but the same genre as the logged correction.
    candidates = [
        CandidatePlaylist(
            id=original_playlist.id, name="Playlist 1", rule=None, description=None, artist_counts={}
        ),
        CandidatePlaylist(
            id=corrected_playlist.id, name="Playlist 2", rule=None, description=None, artist_counts={}
        ),
    ]
    track_b = {"title": "Song B", "artists": [{"name": "A Totally Different Artist"}]}

    feedback_service = _service(
        genre="synthpop", correction_log_repo=CorrectionLogRepository(session)
    )
    result = feedback_service.classify_track(track_b, candidates, user_id=user.id)

    assert result.playlist_id == corrected_playlist.id
    assert result.explanation.signal == "correction_feedback"


def test_correction_log_repo_none_leaves_behavior_unchanged(session):
    # A ruled playlist's outcome must never be influenced by corrections
    # (KTD10 untouched), even when a correction for the same artist exists.
    user, original_playlist, corrected_playlist = _seed_correction(
        session, artist_bucket_key="artistx"
    )
    ruled = CandidatePlaylist(
        id=999, name="High Energy", rule={"bpm_min": 150}, description=None, artist_counts={}
    )
    service_without_repo = _service()
    service_with_repo = _service(correction_log_repo=CorrectionLogRepository(session))

    track = {"title": "Slow Song", "artists": [{"name": "Artist X"}]}
    result_without = service_without_repo.classify_track(track, [ruled])
    result_with = service_with_repo.classify_track(track, [ruled], user_id=user.id)

    # Rule rejects (bpm is None here, so bpm_min=150 fails) regardless of the
    # correction-log repo being wired in.
    assert result_without.playlist_id is None
    assert result_with.playlist_id is None
    assert result_without.explanation.signal == "none"
    assert result_with.explanation.signal == "none"


def test_logging_a_correction_does_not_retroactively_touch_an_unrelated_pending_item(session):
    """KTC12/KTD12 scope check: an unrelated ReviewQueueItem already sitting in
    the queue must be completely unchanged after a new correction is logged
    elsewhere — a correction is read-only with respect to every row except
    the one new correction_log_entry it inserts.
    """
    user = User(display_name="Another User")
    session.add(user)
    session.commit()
    session.refresh(user)

    playlist = Playlist(user_id=user.id, name="Untouched Playlist", rule=None, description=None)
    session.add(playlist)
    session.commit()
    session.refresh(playlist)

    library_item = LibraryItem(
        user_id=user.id, video_id="unrelated-song", title="Unrelated Song", artist="Someone Else"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    pending_item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_item.id,
        playlist_id=playlist.id,
        status="pending",
        confidence=0.73,
        explanation={"signal": "artist_similarity", "detail": "pre-existing suggestion"},
    )
    review_queue_repo = ReviewQueueRepository(session)
    pending_item = review_queue_repo.create(pending_item)

    snapshot = (
        pending_item.status,
        pending_item.playlist_id,
        pending_item.confidence,
        pending_item.version,
    )

    # An entirely separate correction gets logged (its own user/playlists/
    # library item/queue item chain) — nothing about it references
    # `pending_item` at all.
    _seed_correction(session, artist_bucket_key="unrelated-bucket")

    refreshed = review_queue_repo.get(pending_item.id)
    assert (
        refreshed.status,
        refreshed.playlist_id,
        refreshed.confidence,
        refreshed.version,
    ) == snapshot


def test_correction_log_entry_is_append_only_at_the_db_layer(session):
    user, original_playlist, corrected_playlist = _seed_correction(
        session, artist_bucket_key="artistx"
    )
    repo = CorrectionLogRepository(session)
    entries = repo.list_for_user(user.id)
    assert len(entries) == 1
    entry = entries[0]

    def _row_count() -> int:
        return session.execute(select(func.count()).select_from(CorrectionLogEntry)).scalar_one()

    count_before = _row_count()

    # A raw UPDATE against the existing row must be rejected by the
    # `correction_log_entry_no_update` trigger from migration 1 — not merely
    # by the repository's convention of not exposing an update method.
    with pytest.raises(SQLAlchemyError, match="append-only"):
        session.execute(
            update(CorrectionLogEntry)
            .where(CorrectionLogEntry.id == entry.id)
            .values(corrected_playlist_id=original_playlist.id)
        )
        session.commit()
    session.rollback()

    # A raw DELETE against the existing row must be rejected by the
    # `correction_log_entry_no_delete` trigger.
    with pytest.raises(SQLAlchemyError, match="append-only"):
        session.delete(session.get(CorrectionLogEntry, entry.id))
        session.commit()
    session.rollback()

    # Row count is monotonically non-decreasing across the failed
    # UPDATE/DELETE attempts, and the row itself is untouched.
    count_after = _row_count()
    assert count_after == count_before

    still_there = session.get(CorrectionLogEntry, entry.id)
    assert still_there is not None
    assert still_there.corrected_playlist_id == corrected_playlist.id
