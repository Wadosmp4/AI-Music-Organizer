from typing import Optional

from sqlmodel import Session, select

from app.models.user import User


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, user_id: int) -> Optional[User]:
        return self.session.get(User, user_id)

    def create(self, user: User) -> User:
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user
