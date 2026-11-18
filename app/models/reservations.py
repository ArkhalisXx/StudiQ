"""Reservation history and maintenance records."""
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Booking(Base):
    __tablename__ = 'bookings'
    __table_args__ = (
        CheckConstraint("status IN ('pending_payment', 'confirmed', 'checked_in', 'completed', 'cancelled', 'expired')"),
        CheckConstraint('price_minor >= 0'),
        CheckConstraint("slot_policy IN ('single', 'day')"),
        Index('ix_bookings_customer_date', 'customer_id', 'booking_date'),
        Index('ix_bookings_hold_expiry', 'status', 'hold_expires_at'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    customer_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    requested_type_id: Mapped[int] = mapped_column(ForeignKey('workspace_types.id'))
    package_id: Mapped[int] = mapped_column(ForeignKey('packages.id'))
    workspace_id: Mapped[int] = mapped_column(ForeignKey('workspaces.id'))
    booking_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default='pending_payment')
    price_minor: Mapped[int] = mapped_column(Integer)
    package_name: Mapped[str] = mapped_column(String(80))
    slot_policy: Mapped[str] = mapped_column(String(20))
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BookingSlot(Base):
    __tablename__ = 'booking_slots'

    booking_id: Mapped[str] = mapped_column(ForeignKey('bookings.id', ondelete='CASCADE'), primary_key=True)
    time_slot_id: Mapped[int] = mapped_column(ForeignKey('time_slots.id'), primary_key=True)


class MaintenanceBlock(Base):
    __tablename__ = 'maintenance_blocks'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workspace_id: Mapped[int] = mapped_column(ForeignKey('workspaces.id'))
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(String(240))
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MaintenanceSlot(Base):
    __tablename__ = 'maintenance_slots'

    block_id: Mapped[str] = mapped_column(ForeignKey('maintenance_blocks.id', ondelete='CASCADE'), primary_key=True)
    time_slot_id: Mapped[int] = mapped_column(ForeignKey('time_slots.id'), primary_key=True)
