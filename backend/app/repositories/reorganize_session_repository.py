from typing import Optional

from sqlmodel import Session, select

from app.models.library import LibraryItem
from app.models.reorganize_session import ReorganizeSession
from app.models.review_queue import ReviewQueueItem
from app.repositories.base import save

# A review_queue_item in one of these statuses reflects a finished outcome
# for that (library_item, playlist) pair -- written to YouTube, explicitly
# rejected, or the source song went stale. Anything else (`pending`,
# `approved_pending_apply`) is still open work for get_open_for_user's reuse
# check (KTD2).
_TERMINAL_ITEM_STATUSES = {"approved", "moved", "rejected", "stale"}


class ReorganizeSessionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, session_id: int) -> Optional[ReorganizeSession]:
        return self.session.get(ReorganizeSession, session_id)

    def create(self, reorganize_session: ReorganizeSession) -> ReorganizeSession:
        return save(self.session, reorganize_session)

    def update(self, reorganize_session: ReorganizeSession) -> ReorganizeSession:
        return save(self.session, reorganize_session)

    def get_open_for_user(self, user_id: int) -> Optional[ReorganizeSession]:
        """Returns the user's most recent ReorganizeSession if it is still
        "unresolved" (KTD2): clustering hasn't finished, an apply is
        currently running, some session-tagged review_queue_item is still
        pending/approved_pending_apply, or the session's snapshot hasn't
        been fully matched yet. Returns None otherwise, so a new trigger
        starts a fresh session rather than reusing a fully-settled one.
        """
        sessions = list(
            self.session.exec(
                select(ReorganizeSession)
                .where(ReorganizeSession.user_id == user_id)
                .order_by(ReorganizeSession.id.desc())
            )
        )
        if not sessions:
            return None
        latest = sessions[0]
        return latest if self._is_unresolved(latest) else None

    def _is_unresolved(self, reorganize_session: ReorganizeSession) -> bool:
        if reorganize_session.clustering_status in ("pending", "in_progress", "stalled"):
            return True
        if reorganize_session.apply_status == "in_progress":
            return True

        tagged_items = list(
            self.session.exec(
                select(ReviewQueueItem).where(
                    ReviewQueueItem.reorganize_session_id == reorganize_session.id
                )
            )
        )
        if any(item.status not in _TERMINAL_ITEM_STATUSES for item in tagged_items):
            return True

        matched_library_item_ids = {item.library_item_id for item in tagged_items}
        if not matched_library_item_ids:
            return bool(reorganize_session.video_id_snapshot)
        matched_video_ids = {
            library_item.video_id
            for library_item in self.session.exec(
                select(LibraryItem).where(LibraryItem.id.in_(matched_library_item_ids))
            )
        }
        return len(matched_video_ids) < len(reorganize_session.video_id_snapshot)
