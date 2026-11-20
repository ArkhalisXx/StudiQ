"""Immutable rendered receipt snapshots with retryable delivery state."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmailReceipt(Base):
    __tablename__ = 'email_receipts'
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sending', 'sent', 'preview', 'failed')"),
        CheckConstraint('attempts >= 0'),
        CheckConstraint("delivery_mode IS NULL OR delivery_mode IN ('file', 'smtp')"),
        Index('ix_email_receipts_delivery', 'status', 'next_attempt_at'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    attempt_id: Mapped[str] = mapped_column(ForeignKey('payment_attempts.id'), unique=True)
    booking_id: Mapped[str] = mapped_column(ForeignKey('bookings.id'), index=True)
    recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(200))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default='pending')
    delivery_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(240), nullable=True)
    message_id: Mapped[str] = mapped_column(String(200), default=lambda: f'<{uuid4()}@studiq.local>')
