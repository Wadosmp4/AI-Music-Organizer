from sqlmodel import Session, select

from app.models.onboarding_proposal import OnboardingProposal
from app.repositories.base import save


class OnboardingProposalRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_for_user(self, user_id: int) -> list[OnboardingProposal]:
        return list(
            self.session.exec(
                select(OnboardingProposal)
                .where(OnboardingProposal.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        )

    def clear_for_user(self, user_id: int) -> None:
        """Drops this user's prior proposal rows -- called at the start of a
        fresh run so a re-trigger doesn't just keep accumulating stale
        suggestions from a previous library snapshot alongside new ones."""
        for proposal in self.list_for_user(user_id):
            self.session.delete(proposal)
        self.session.commit()

    def merge_proposal(self, user_id: int, name: str, theme: str, count: int) -> OnboardingProposal:
        """Mirrors ReorganizeSessionRepository.merge_proposal: a proposal
        already surfaced for this user under the same name (case-insensitive)
        has its song_count accumulated; a new name gets a fresh row."""
        key = name.strip().lower()
        for proposal in self.list_for_user(user_id):
            if proposal.name.strip().lower() == key:
                proposal.song_count += count
                return save(self.session, proposal)
        return save(
            self.session,
            OnboardingProposal(user_id=user_id, name=name, theme=theme, song_count=count),
        )
