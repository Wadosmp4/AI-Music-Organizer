from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.unplaced import unplaced_library_items


def _make_user(session) -> User:
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_a_pending_item_with_no_playlist_match_still_counts_as_unplaced(session):
    """Regression: jobs/ingestion.py now creates a pending review_queue_item
    even when a song matches no playlist (playlist_id=None), so it can
    surface in the Review Queue's "Unassigned" group. Before this fix, that
    alone made unplaced_library_items treat the song as "placed" -- starving
    the new-playlist clustering proposal of every unmatched song, which is
    exactly the backlog it's meant to find patterns in."""
    user = _make_user(session)
    library_repo = LibraryRepository(session)
    queue_repo = ReviewQueueRepository(session)
    item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_repo.create(ReviewQueueItem(user_id=user.id, library_item_id=item.id, playlist_id=None))

    unplaced = unplaced_library_items(user.id, library_repo, queue_repo)

    assert [i.id for i in unplaced] == [item.id]


def test_a_pending_item_matched_to_a_playlist_is_placed(session):
    user = _make_user(session)
    library_repo = LibraryRepository(session)
    playlist_repo = PlaylistRepository(session)
    queue_repo = ReviewQueueRepository(session)
    playlist = playlist_repo.create(Playlist(user_id=user.id, name="Rock", description=None, rule=None))
    item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=item.id, playlist_id=playlist.id)
    )

    unplaced = unplaced_library_items(user.id, library_repo, queue_repo)

    assert unplaced == []


def test_a_rejected_item_is_still_unplaced(session):
    user = _make_user(session)
    library_repo = LibraryRepository(session)
    playlist_repo = PlaylistRepository(session)
    queue_repo = ReviewQueueRepository(session)
    playlist = playlist_repo.create(Playlist(user_id=user.id, name="Rock", description=None, rule=None))
    item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_repo.create(
        ReviewQueueItem(
            user_id=user.id, library_item_id=item.id, playlist_id=playlist.id, status="rejected"
        )
    )

    unplaced = unplaced_library_items(user.id, library_repo, queue_repo)

    assert [i.id for i in unplaced] == [item.id]
