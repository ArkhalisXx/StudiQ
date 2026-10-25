"""Initial inventory model. Future booking writes must use SlotAllocation transactions."""
from datetime import date, time

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Integer, String, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkspaceType(Base):
    __tablename__ = 'workspace_types'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(20))


class Workspace(Base):
    __tablename__ = 'workspaces'
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_type_id: Mapped[int] = mapped_column(ForeignKey('workspace_types.id'))
    code: Mapped[str] = mapped_column(String(40), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class TimeSlot(Base):
    __tablename__ = 'time_slots'
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(40), unique=True)
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)


class Package(Base):
    __tablename__ = 'packages'
    __table_args__ = (
        CheckConstraint('price_minor >= 0'),
        CheckConstraint("slot_policy IN ('single', 'day')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    price_minor: Mapped[int] = mapped_column(Integer)
    slot_policy: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SlotAllocation(Base):
    __tablename__ = 'slot_allocations'
    __table_args__ = (
        UniqueConstraint('booking_date', 'workspace_id', 'time_slot_id'),
        CheckConstraint('(booking_id IS NOT NULL) <> (maintenance_block_id IS NOT NULL)'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_date: Mapped[date] = mapped_column(Date)
    workspace_id: Mapped[int] = mapped_column(ForeignKey('workspaces.id'))
    time_slot_id: Mapped[int] = mapped_column(ForeignKey('time_slots.id'))
    # Future migrations will add booking/maintenance tables and foreign keys.
    booking_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    maintenance_block_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
