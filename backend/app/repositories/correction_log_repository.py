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
