from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Package, TimeSlot, WorkspaceType
from app.services.availability import available_count

router = APIRouter(prefix='/api')


def local_today():
    return datetime.now(ZoneInfo('Asia/Manila')).date()


@router.get('/packages')
def packages(session: Session = Depends(get_session)):
    return [dict(id=p.id, name=p.name, price_minor=p.price_minor, currency='PHP', slot_policy=p.slot_policy)
            for p in session.scalars(select(Package).where(Package.is_active.is_(True)).order_by(Package.id))]


@router.get('/workspace-types')
def workspace_types(session: Session = Depends(get_session)):
    return [dict(id=t.id, name=t.name, kind=t.kind) for t in session.scalars(select(WorkspaceType).order_by(WorkspaceType.id))]


@router.get('/time-slots')
def time_slots(session: Session = Depends(get_session)):
    return [dict(id=s.id, label=s.label, starts_at=s.starts_at.strftime('%H:%M'), ends_at=s.ends_at.strftime('%H:%M'))
            for s in session.scalars(select(TimeSlot).order_by(TimeSlot.starts_at))]


@router.get('/workspaces/availability')
def availability(
    booking_date: date,
    workspace_type_id: int = Query(gt=0),
    package_id: int = Query(gt=0),
    time_slot_id: int | None = Query(default=None, gt=0),
    session: Session = Depends(get_session),
):
    if booking_date < local_today():
        raise HTTPException(422, 'Choose today or a future date.')
    category = session.get(WorkspaceType, workspace_type_id)
    package = session.get(Package, package_id)
    if category is None or package is None or not package.is_active:
        raise HTTPException(404, 'Workspace type or active pass not found.')
    slots = session.scalars(select(TimeSlot).order_by(TimeSlot.starts_at)).all()
    if package.slot_policy == 'single':
        slots = [slot for slot in slots if slot.id == time_slot_id]
        if not slots:
            raise HTTPException(422, 'Select a valid time slot for the 3-Hour Pass.')
    count = available_count(session, booking_date, category.id, [slot.id for slot in slots])
    return dict(booking_date=booking_date, workspace_type=category.name, package=package.name,
                available=count, slots=[slot.label for slot in slots], reservable=False,
                message='Availability only. Booking and checkout are coming in a later stage.')
