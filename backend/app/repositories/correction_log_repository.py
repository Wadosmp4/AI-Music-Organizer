from sqlmodel import Session, select

from app.models.correction_log import CorrectionLogEntry


class CorrectionLogRepository:
    """Append-only: intentionally exposes no update/delete methods.

    DB-level immutability (KTD20) is enforced by triggers added in the
    initial migration, so even a raw SQL statement bypassing this
    repository cannot mutate or delete an existing row.
    """

    def __init__(self, session: Session):
        self.session = session

    def create(self, entry: CorrectionLogEntry) -> CorrectionLogEntry:
        self.session.add(entry)
        self.session.commit()
        self.session.refresh(entry)
        return entry

    def list_for_user(self, user_id: int) -> list[CorrectionLogEntry]:
        return list(
            self.session.exec(
                select(CorrectionLogEntry).where(CorrectionLogEntry.user_id == user_id)
            )
        )

    def list_recent_for_user(self, user_id: int, limit: int = 20) -> list[CorrectionLogEntry]:
        """Bounded recent-N corrections for a user (KTD12), most recent first.

        Deliberately not `list_for_user`'s full history: feedback into future
        classification is scoped to a fixed recent-N so a single stale
        correction can't dominate forever, and old corrections naturally age
        out as new ones are logged.
        """
        return list(
            self.session.exec(
                select(CorrectionLogEntry)
                .where(CorrectionLogEntry.user_id == user_id)
                .order_by(CorrectionLogEntry.id.desc())
                .limit(limit)
            )
        )
