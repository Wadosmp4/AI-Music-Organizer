from typing import Optional

from sqlmodel import Session

from app.models.base import utcnow
from app.models.user import User
from app.repositories.base import save


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, user_id: int) -> Optional[User]:
        return self.session.get(User, user_id)

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
