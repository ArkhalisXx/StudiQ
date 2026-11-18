from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import same_origin
from app.database import get_session
from app.models import Booking, User
from app.services.auth import require_user
from app.services.reservations import (begin_write, cancel_booking, create_booking,
                                       expire_holds, serialize_booking)

MAX_CATALOG_ID = 2**63 - 1
router = APIRouter(prefix='/api/bookings', tags=['Reservations'], dependencies=[Depends(require_user)])


class BookingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    booking_date: date
    workspace_type_id: int = Field(gt=0, le=MAX_CATALOG_ID, strict=True)
    package_id: int = Field(gt=0, le=MAX_CATALOG_ID, strict=True)
    time_slot_id: int | None = Field(default=None, gt=0, le=MAX_CATALOG_ID, strict=True)


@router.post('', status_code=201, dependencies=[Depends(same_origin)])
def reserve(data: BookingInput, user: User = Depends(require_user), session: Session = Depends(get_session)):
    customer_id = user.id
    booking = create_booking(session, customer_id, data)
    return serialize_booking(session, booking, customer_id)


@router.get('/me')
def my_bookings(user: User = Depends(require_user), session: Session = Depends(get_session)):
    customer_id = user.id
    with begin_write(session):
        expire_holds(session)
        records = session.scalars(select(Booking).where(Booking.customer_id == customer_id)
                                  .order_by(Booking.booking_date.desc(), Booking.created_at.desc(), Booking.id)).all()
        return [serialize_booking(session, booking, customer_id) for booking in records]


@router.get('/{booking_id}')
def booking_detail(booking_id: UUID, user: User = Depends(require_user), session: Session = Depends(get_session)):
    customer_id = user.id
    with begin_write(session):
        expire_holds(session)
        booking = session.scalar(select(Booking).where(Booking.id == str(booking_id),
                                                       Booking.customer_id == customer_id))
        if booking is None:
            raise HTTPException(404, 'Reservation not found.')
        return serialize_booking(session, booking, customer_id)


@router.post('/{booking_id}/cancel', dependencies=[Depends(same_origin)])
def cancel(booking_id: UUID, user: User = Depends(require_user), session: Session = Depends(get_session)):
    customer_id = user.id
    booking = cancel_booking(session, str(booking_id), customer_id)
    return serialize_booking(session, booking, customer_id)
