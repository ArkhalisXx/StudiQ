"""Hosted payment attempts, active checkout locks, and webhook deduplication."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PaymentAttempt(Base):
    __tablename__ = 'payment_attempts'
    __table_args__ = (
        CheckConstraint("status IN ('creating', 'pending', 'paid', 'failed', 'cancelled', 'expired', 'review')"),
        CheckConstraint('amount_minor >= 0'),
        CheckConstraint("mode IN ('test', 'live')"),
        CheckConstraint('length(currency) = 3'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    booking_id: Mapped[str] = mapped_column(ForeignKey('bookings.id'), index=True)
    provider: Mapped[str] = mapped_column(String(40), default='paymongo')
    status: Mapped[str] = mapped_column(String(20), default='creating')
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default='PHP')
    mode: Mapped[str] = mapped_column(String(4), default='test')
    provider_checkout_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    provider_payment_intent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)


class ActiveCheckout(Base):
    __tablename__ = 'active_checkouts'

    booking_id: Mapped[str] = mapped_column(ForeignKey('bookings.id'), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey('payment_attempts.id'), unique=True)


class PaymentEvent(Base):
    __tablename__ = 'payment_events'

    provider_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey('payment_attempts.id'), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    outcome: Mapped[str] = mapped_column(String(80), default='received')
