"""Shared 'unplaced library item' filtering.

Used by onboarding library analysis (U9) and natural-language playlist
creation (U6) alike: a library item is unplaced when it has no active
(non-rejected/non-stale) review_queue_item actually pointing it at a
playlist. A pending item with playlist_id=None (jobs/ingestion.py creates
one for every classified song now, matched or not, so it can surface in the
Review Queue's "Unassigned" group) isn't placed anywhere -- it must stay
eligible here too, or a song sitting unassigned could never be proposed as
part of a new-playlist cluster.
"""

from app.models.library import LibraryItem
from app.repositories.library_repository import LibraryRepository
from app.repositories.review_queue_repository import ReviewQueueRepository

PLACED_STATUSES = {"pending", "write_pending", "approved", "moved", "approved_pending_apply"}


def unplaced_library_items(
    user_id: int,
    library_repository: LibraryRepository,
    review_queue_repository: ReviewQueueRepository,
) -> list[LibraryItem]:
    placed_ids = {
        item.library_item_id
        for item in review_queue_repository.list_for_user(user_id)
        if item.status in PLACED_STATUSES and item.playlist_id is not None
    }
    return [
        item for item in library_repository.list_for_user(user_id) if item.id not in placed_ids
    ]
