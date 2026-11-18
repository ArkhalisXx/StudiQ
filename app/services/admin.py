"""Staff operations for reservations, allocations and maintenance."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import (AdminGrant, Booking, MaintenanceBlock, MaintenanceSlot,
                        SlotAllocation, TimeSlot, User, Workspace, WorkspaceType)
from app.services.auth import require_user, utc_now
from app.services.reservations import (begin_write, booking_slots, expire_holds,
                                       serialize_booking)

MANILA = ZoneInfo('Asia/Manila')


def has_admin_access(session, user):
    return user is not None and session.get(AdminGrant, user.id) is not None


def require_admin(user: User = Depends(require_user), session: Session = Depends(get_session)):
    if not has_admin_access(session, user):
        raise HTTPException(403, 'Administrator access is required.')
    return user


def local_now():
    return utc_now().replace(tzinfo=timezone.utc).astimezone(MANILA)


def slot_time(day, value):
    return datetime.combine(day, value, MANILA).astimezone(timezone.utc).replace(tzinfo=None)


def finish_past_visits(session):
    now = utc_now()
    visits = session.scalars(select(Booking).where(Booking.status == 'checked_in')).all()
    for booking in visits:
        slots = booking_slots(session, booking.id)
        if slots:
            end = slot_time(booking.booking_date, max(s.ends_at for s in slots))
            if end <= now:
                booking.status = 'completed'
                booking.completed_at = end


def refresh_operations(session):
    with begin_write(session):
        expire_holds(session)
        finish_past_visits(session)


def booking_actions(session, booking):
    slots = booking_slots(session, booking.id)
    now = utc_now()
    start = slot_time(booking.booking_date, min(s.starts_at for s in slots)) if slots else None
    end = slot_time(booking.booking_date, max(s.ends_at for s in slots)) if slots else None
    return {
        'can_confirm': bool(booking.status == 'pending_payment' and start and now < start
                            and booking.hold_expires_at and now < booking.hold_expires_at),
        'can_check_in': bool(booking.status == 'confirmed' and start and end and start <= now < end),
        'can_complete': booking.status == 'checked_in',
        'can_cancel': bool(booking.status in {'pending_payment', 'confirmed'} and end and now < end),
    }


def roster(session, booking_date):
    refresh_operations(session)
    records = session.scalars(select(Booking).where(Booking.booking_date == booking_date)
                              .order_by(Booking.created_at, Booking.id)).all()
    rows = []
    for booking in records:
        customer = session.get(User, booking.customer_id)
        row = serialize_booking(session, booking)
        row.update(customer_name=customer.full_name, customer_email=customer.email)
        row.update(booking_actions(session, booking))
        rows.append(row)
    # The visit start is the useful roster order; creation order breaks ties.
    rows.sort(key=lambda row: (row['slots'][0]['starts_at'] if row['slots'] else '', row['created_at']))
    return rows


def allocation_grid(session, booking_date):
    refresh_operations(session)
    slots = session.scalars(select(TimeSlot).order_by(TimeSlot.starts_at)).all()
    allocations = session.scalars(select(SlotAllocation).where(
        SlotAllocation.booking_date == booking_date)).all()
    by_position = {(a.workspace_id, a.time_slot_id): a for a in allocations}
    rows = []
    for workspace in session.scalars(select(Workspace).order_by(Workspace.code)):
        category = session.get(WorkspaceType, workspace.workspace_type_id)
        cells = []
        for slot in slots:
            allocation = by_position.get((workspace.id, slot.id))
            cell = {'slot_id': slot.id, 'status': 'available'}
            if allocation and allocation.booking_id:
                booking = session.get(Booking, allocation.booking_id)
                customer = session.get(User, booking.customer_id)
                cell.update(status=booking.status, booking_id=booking.id,
                            customer_name=customer.full_name)
            elif allocation and allocation.maintenance_block_id:
                block = session.get(MaintenanceBlock, allocation.maintenance_block_id)
                cell.update(status='maintenance', maintenance_block_id=block.id, reason=block.reason)
            cells.append(cell)
        rows.append({'id': workspace.id, 'code': workspace.code, 'workspace_type': category.name,
                     'is_active': workspace.is_active, 'allocations': cells})
    return {'booking_date': booking_date, 'slots': [
        {'id': slot.id, 'label': slot.label, 'starts_at': slot.starts_at.strftime('%H:%M'),
         'ends_at': slot.ends_at.strftime('%H:%M')} for slot in slots], 'workspaces': rows}


def change_booking(session, booking_id, admin_id, action):
    # Commit housekeeping before a rejected action, so expired holds stay released.
    refresh_operations(session)
    with begin_write(session):
        booking = session.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
        if booking is None:
            raise HTTPException(404, 'Reservation not found.')
        actions = booking_actions(session, booking)
        now = utc_now()
        if action == 'confirm':
            if booking.status == 'confirmed':
                return booking
            if not actions['can_confirm']:
                raise HTTPException(409, 'This hold can no longer be confirmed. Please make a new reservation.')
            # Staff confirmation reserves the visit for on-site payment. No payment is marked paid.
            booking.status = 'confirmed'
            booking.confirmed_at = now
            booking.confirmed_by = admin_id
            booking.hold_expires_at = None
        elif action == 'check-in':
            if booking.status == 'checked_in':
                return booking
            if not actions['can_check_in']:
                raise HTTPException(409, 'Only a confirmed reservation during its visit time can be checked in.')
            booking.status = 'checked_in'
            booking.checked_in_at = now
        elif action == 'complete':
            if booking.status == 'completed':
                return booking
            if not actions['can_complete']:
                raise HTTPException(409, 'Only a checked-in reservation can be completed.')
            booking.status = 'completed'
            booking.completed_at = now
        elif action == 'cancel':
            if booking.status == 'cancelled':
                return booking
            if not actions['can_cancel']:
                raise HTTPException(409, 'This reservation cannot be cancelled.')
            booking.status = 'cancelled'
            booking.cancelled_at = now
            booking.hold_expires_at = None
            for allocation in session.scalars(select(SlotAllocation).where(
                    SlotAllocation.booking_id == booking.id)):
                session.delete(allocation)
        else:
            raise HTTPException(404, 'Unknown reservation action.')
    return booking


def block_workspace(session, admin_id, data):
    refresh_operations(session)
    try:
        with begin_write(session):
            workspace = session.get(Workspace, data.workspace_id)
            if workspace is None or not workspace.is_active:
                raise HTTPException(404, 'Active workspace not found.')
            if data.booking_date < local_now().date():
                raise HTTPException(422, 'Choose today or a future date.')
            slots = session.scalars(select(TimeSlot).where(TimeSlot.id.in_(data.time_slot_ids))).all()
            if len(slots) != len(data.time_slot_ids):
                raise HTTPException(422, 'Select valid time slots.')
            if any(slot_time(data.booking_date, slot.ends_at) <= utc_now() for slot in slots):
                raise HTTPException(422, 'A selected time slot has already ended.')
            occupied = session.scalar(select(SlotAllocation.id).where(
                SlotAllocation.workspace_id == workspace.id,
                SlotAllocation.booking_date == data.booking_date,
                SlotAllocation.time_slot_id.in_(data.time_slot_ids)))
            if occupied is not None:
                raise HTTPException(409, 'This workspace already has a reservation or maintenance block in those slots.')
            block = MaintenanceBlock(workspace_id=workspace.id, booking_date=data.booking_date,
                                     reason=data.reason, created_by=admin_id, created_at=utc_now())
            session.add(block)
            session.flush()
            for slot in slots:
                session.add(MaintenanceSlot(block_id=block.id, time_slot_id=slot.id))
                session.add(SlotAllocation(booking_date=data.booking_date, workspace_id=workspace.id,
                                          time_slot_id=slot.id, maintenance_block_id=block.id))
    except IntegrityError:
        raise HTTPException(409, 'A selected slot was just allocated. Refresh and try again.') from None
    return {'id': block.id, 'workspace_id': block.workspace_id, 'booking_date': block.booking_date,
            'reason': block.reason, 'time_slot_ids': data.time_slot_ids}


def release_block(session, block_id):
    with begin_write(session):
        block = session.get(MaintenanceBlock, block_id)
        if block is None:
            raise HTTPException(404, 'Maintenance block not found.')
        if block.released_at is None:
            for allocation in session.scalars(select(SlotAllocation).where(
                    SlotAllocation.maintenance_block_id == block.id)):
                session.delete(allocation)
            block.released_at = utc_now()
    return {'id': block.id, 'released': True}


def members(session):
    refresh_operations(session)
    totals = {}
    completed = {}
    for customer_id, status, count in session.execute(select(
            Booking.customer_id, Booking.status, func.count()).group_by(Booking.customer_id, Booking.status)):
        totals[customer_id] = totals.get(customer_id, 0) + count
        if status == 'completed':
            completed[customer_id] = count
    return [{'id': user.id, 'full_name': user.full_name, 'email': user.email,
             'completed_bookings': completed.get(user.id, 0), 'total_bookings': totals.get(user.id, 0)}
            for user in session.scalars(select(User).order_by(User.full_name, User.id))]
