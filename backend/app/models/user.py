from typing import Optional

from sqlmodel import Field

from app.models.base import TimestampMixin


class User(TimestampMixin, table=True):
    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    display_name: str
