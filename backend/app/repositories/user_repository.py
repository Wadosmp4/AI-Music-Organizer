from typing import Optional

from sqlmodel import Session

from app.models.base import utcnow
from app.models.user import User
from app.repositories.base import save


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, user_id: int) -> Optional[User]:
        # populate_existing=True: this row may have been committed by a
        # different Session (e.g. the background ingestion-check task, which
        # opens its own DB session, same as ReorganizeSessionRepository.get)
        # -- without it, a caller that already loaded this row earlier in the
        # same Session would get back its own stale identity-mapped copy
        # instead of the row's current state.
        return self.session.get(User, user_id, populate_existing=True)

    def create(self, user: User) -> User:
        return save(self.session, user)

    def _mark_timestamp(self, user_id: int, field: str) -> User:
        user = self.get(user_id)
        setattr(user, field, utcnow())
        return save(self.session, user)

    def mark_onboarding_completed(self, user_id: int) -> User:
        return self._mark_timestamp(user_id, "onboarding_completed_at")

    def mark_backfill_completed(self, user_id: int) -> User:
        return self._mark_timestamp(user_id, "backfill_completed_at")

    def reset_backfill(self, user_id: int) -> User:
        """Clears backfill_completed_at so the next ingestion check restarts
        backfill from the beginning of the liked-songs list, instead of
        resuming steady-state from wherever the user left off."""
        user = self.get(user_id)
        user.backfill_completed_at = None
        return save(self.session, user)

    def set_ingestion_progress(
        self,
        user_id: int,
        *,
        status: Optional[str] = None,
        processed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> User:
        """Persists background ingestion-check progress (mirrors
        ReorganizeSessionRepository's matching_status/matched_count updates)
        so the status-poll endpoint has something durable to read. Each
        field is only touched when explicitly passed, so a caller updating
        just `processed` after a batch doesn't have to re-read+repass the
        other two."""
        user = self.get(user_id)
        if status is not None:
            user.ingestion_status = status
        if processed is not None:
            user.ingestion_processed_count = processed
        if total is not None:
            user.ingestion_total_count = total
        return save(self.session, user)

    def set_proposals_progress(
        self,
        user_id: int,
        *,
        status: Optional[str] = None,
        processed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> User:
        """Persists background onboarding-proposals generation progress --
        same shape/rationale as set_ingestion_progress."""
        user = self.get(user_id)
        if status is not None:
            user.proposals_status = status
        if processed is not None:
            user.proposals_processed_count = processed
        if total is not None:
            user.proposals_total_count = total
        return save(self.session, user)
