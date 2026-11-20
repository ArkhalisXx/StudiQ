"""Customer reservations and the shared workspace allocation ledger."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.models import Booking, BookingSlot, Package, SlotAllocation, TimeSlot, Workspace, WorkspaceType
from app.services.auth import utc_now

MANILA = ZoneInfo('Asia/Manila')
HOLD_MINUTES = 15


@contextmanager
def begin_write(session):
    """Start a write transaction after discarding the request's authentication reads."""
    if session.info.get('reservation_write'):
        yield
        return
    if session.new or session.dirty or session.deleted:
        raise RuntimeError('Finish pending database changes before starting a reservation transaction.')
    if session.in_transaction():
        session.rollback()
    session.info['reservation_write'] = True
    try:
        if session.get_bind().dialect.name == 'sqlite':
            # SQLite serializes inventory decisions across processes, not just threads.
            session.execute(text('BEGIN IMMEDIATE'))
        else:
            session.begin()
        yield
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.info.pop('reservation_write', None)


def slot_start_utc(booking_date, slot):
    return datetime.combine(booking_date, slot.starts_at, MANILA).astimezone(timezone.utc).replace(tzinfo=None)


def slots_have_started(booking_date, slots, now=None):
    now = now or utc_now()
    return any(slot_start_utc(booking_date, slot) <= now for slot in slots)


def selected_slots(session, package, time_slot_id=None):
    slots = session.scalars(select(TimeSlot).order_by(TimeSlot.starts_at, TimeSlot.id)).all()
    if package.slot_policy == 'day':
        if len(slots) != 2:
            raise HTTPException(503, 'The daily schedule is unavailable. Please try again later.')
        return slots
    if package.slot_policy == 'single':
        chosen = [slot for slot in slots if slot.id == time_slot_id]
        if not chosen:
            raise HTTPException(422, 'Select a valid time slot for the 3-Hour Pass.')
        return chosen
    raise HTTPException(503, 'This pass is not available. Please choose another pass.')


def booking_slots(session, booking_id):
    return session.scalars(select(TimeSlot).join(BookingSlot, BookingSlot.time_slot_id == TimeSlot.id)
                           .where(BookingSlot.booking_id == booking_id)
                           .order_by(TimeSlot.starts_at, TimeSlot.id)).all()


def expire_holds(session, now=None):
    """Release expired holds inside the caller's write transaction."""
    now = now or utc_now()
    expired = session.scalars(select(Booking).where(
        Booking.status == 'pending_payment', Booking.hold_expires_at <= now).with_for_update()).all()
    if expired:
        ids = [booking.id for booking in expired]
        session.execute(delete(SlotAllocation).where(SlotAllocation.booking_id.in_(ids)))
        for booking in expired:
            booking.status = 'expired'
        session.flush()
    return len(expired)


def utc_string(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def serialize_booking(session, booking, user_id=None):
    workspace = session.get(Workspace, booking.workspace_id)
    category = session.get(WorkspaceType, booking.requested_type_id)
    slots = booking_slots(session, booking.id)
    now = utc_now()
    can_cancel = (booking.status in {'pending_payment', 'confirmed'} and bool(slots)
                  and not slots_have_started(booking.booking_date, slots, now)
                  and (user_id is None or booking.customer_id == user_id))
    if booking.status == 'pending_payment':
        can_cancel = can_cancel and booking.hold_expires_at is not None and booking.hold_expires_at > now
    from app.services.payments import payment_blocks_changes, payment_summary
    payment = payment_summary(session, booking)
    can_cancel = can_cancel and not payment_blocks_changes(session, booking.id)
    return {
        'id': booking.id, 'booking_date': booking.booking_date, 'status': booking.status,
        'workspace_code': workspace.code, 'workspace_type': category.name,
        'package_name': booking.package_name, 'price_minor': booking.price_minor, 'currency': 'PHP',
        'slots': [{'id': slot.id, 'label': slot.label, 'starts_at': slot.starts_at.strftime('%H:%M'),
                   'ends_at': slot.ends_at.strftime('%H:%M')} for slot in slots],
        'created_at': utc_string(booking.created_at), 'hold_expires_at': utc_string(booking.hold_expires_at),
        'can_cancel': bool(can_cancel), 'payment': payment,
    }


def create_booking(session, customer_id, data):
    try:
        with begin_write(session):
            now = utc_now()
            expire_holds(session, now)
            local_date = now.replace(tzinfo=timezone.utc).astimezone(MANILA).date()
            if data.booking_date < local_date:
                raise HTTPException(422, 'Choose today or a future date.')
            category = session.get(WorkspaceType, data.workspace_type_id)
            package = session.get(Package, data.package_id)
            if category is None or package is None or not package.is_active:
                raise HTTPException(404, 'Workspace type or active pass not found.')
            slots = selected_slots(session, package, data.time_slot_id)
            if slots_have_started(data.booking_date, slots, now):
                raise HTTPException(422, 'A selected time slot has already started. Choose another slot or date.')
            slot_ids = [slot.id for slot in slots]
            occupied = select(SlotAllocation.workspace_id).where(
                SlotAllocation.booking_date == data.booking_date, SlotAllocation.time_slot_id.in_(slot_ids))
            workspace = session.scalar(select(Workspace).where(
                Workspace.workspace_type_id == category.id, Workspace.is_active.is_(True),
                Workspace.id.not_in(occupied)).order_by(Workspace.id).limit(1).with_for_update(skip_locked=True))
            if workspace is None:
                raise HTTPException(409, 'No workspace is available for those slots. Choose another date or pass.')
            expiry = min(now + timedelta(minutes=HOLD_MINUTES), min(slot_start_utc(data.booking_date, slot) for slot in slots))
            booking = Booking(customer_id=customer_id, requested_type_id=category.id,
                              package_id=package.id, workspace_id=workspace.id,
                              booking_date=data.booking_date, status='pending_payment',
                              price_minor=package.price_minor, package_name=package.name,
                              slot_policy=package.slot_policy, hold_expires_at=expiry, created_at=now)
            session.add(booking)
            session.flush()
            for slot in slots:
                session.add(BookingSlot(booking_id=booking.id, time_slot_id=slot.id))
                session.add(SlotAllocation(booking_date=data.booking_date, workspace_id=workspace.id,
                                          time_slot_id=slot.id, booking_id=booking.id))
    except IntegrityError:
        raise HTTPException(409, 'A selected workspace was just reserved. Refresh and try again.') from None
    return booking


def cancel_booking(session, booking_id, customer_id):
    error = None
    with begin_write(session):
        expire_holds(session)
        booking = session.scalar(select(Booking).where(Booking.id == booking_id,
                                 Booking.customer_id == customer_id).with_for_update())
        if booking is None:
            error = HTTPException(404, 'Reservation not found.')
        elif booking.status != 'cancelled':
            if not serialize_booking(session, booking, customer_id)['can_cancel']:
                error = HTTPException(409, 'This reservation can no longer be cancelled.')
            else:
                booking.status = 'cancelled'
                booking.cancelled_at = utc_now()
                booking.hold_expires_at = None
                session.execute(delete(SlotAllocation).where(SlotAllocation.booking_id == booking.id))
    if error:
        raise error
    return booking
