"""Task 23: cross-process state in the existing database (opt-in `SHARED_STATE_BACKEND=database`).

Both tables are used ONLY when that backend is selected; with the default (`memory`) they stay
empty. No client address is stored in clear: `identity` is a SHA-256 hex digest.
"""

from sqlalchemy import BigInteger, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SharedRateLimitHit(Base):
    """One counted request of one limiter for one (hashed) client identity. A sliding window is
    the set of rows newer than `now - window`; rows are deleted when they expire or are refunded."""

    __tablename__ = "shared_rate_limit_hits"
    __table_args__ = (
        Index("ix_shared_rate_limit_hits_identity", "limiter", "identity", "hit_at"),
        Index("ix_shared_rate_limit_hits_limiter_time", "limiter", "hit_at"),
        # ids are refund tokens: SQLite must never hand a deleted id out again
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    limiter: Mapped[str] = mapped_column(String(64), nullable=False)
    identity: Mapped[str] = mapped_column(String(64), nullable=False)
    # Wall-clock epoch seconds: the value must mean the same in every process.
    hit_at: Mapped[float] = mapped_column(Float, nullable=False)


class SharedUiSessionRevocation(Base):
    """A UI session id that logged out, kept until the cookie's own expiry."""

    __tablename__ = "shared_ui_session_revocations"
    __table_args__ = (Index("ix_shared_ui_session_revocations_expires_at", "expires_at"),)

    sid: Mapped[str] = mapped_column(String(32), primary_key=True)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
