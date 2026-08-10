"""Onboarding's initial AI-suggested new-playlist proposals, persisted
incrementally as they're clustered (mirrors reorganize_session.PlaylistProposal,
but keyed by user_id instead of a reorganize_session_id -- onboarding's initial
proposal generation runs once per user, before any reorganize session exists,
so it has no session row to hang off of).
"""

from typing import Optional

from sqlmodel import Field

from app.models.base import TimestampMixin


class OnboardingProposal(TimestampMixin, table=True):
    __tablename__ = "onboarding_proposal"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    name: str
    theme: str
    song_count: int
