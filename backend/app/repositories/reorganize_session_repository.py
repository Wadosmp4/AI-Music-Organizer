from typing import Optional

from sqlmodel import Session, select

from app.models.library import LibraryItem
from app.models.reorganize_session import PlaylistProposal, ReorganizeSession
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
        # populate_existing=True: this row may have been committed by a
        # different Session (e.g. the background clustering task, U2, which
        # opens its own DB session) -- without it, a caller that already
        # loaded this row earlier in the same Session would get back its own
        # stale identity-mapped copy instead of the row's current state.
        return self.session.get(ReorganizeSession, session_id, populate_existing=True)

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

    # -- PlaylistProposal (U2, KTD2) --------------------------------------

    def list_proposals(self, reorganize_session_id: int) -> list[PlaylistProposal]:
        return list(
            self.session.exec(
                select(PlaylistProposal)
                .where(PlaylistProposal.reorganize_session_id == reorganize_session_id)
                .execution_options(populate_existing=True)
            )
        )

    def merge_proposal(self, reorganize_session_id: int, name: str, theme: str, count: int) -> PlaylistProposal:
        """Persists one batch's clustering result as a durable row, replacing
        the transient in-memory `merged` dict `propose_new_playlists` used to
        keep (KTD2): a proposal already surfaced for this session under the
        same name (case-insensitive) has its song_count accumulated; a new
        name gets a fresh row. Visibility (MIN_CLUSTER_SIZE) is applied by
        the reader, not here, so a cluster that only crosses the threshold
        once later batches merge into it isn't lost in between.
        """
        key = name.strip().lower()
        for proposal in self.list_proposals(reorganize_session_id):
            if proposal.name.strip().lower() == key:
                proposal.song_count += count
                return save(self.session, proposal)
        return save(
            self.session,
            PlaylistProposal(
                reorganize_session_id=reorganize_session_id,
                name=name,
                theme=theme,
                song_count=count,
            ),
        )
