from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.auth import same_origin
from app.api.catalog import MAX_CATALOG_ID
from app.database import get_session
from app.models import User
from app.services.admin import (allocation_grid, block_workspace, booking_actions, change_booking,
                                members, release_block, require_admin, roster)
from app.services.reservations import serialize_booking

router = APIRouter(prefix='/api/admin', tags=['Administration'], dependencies=[Depends(require_admin)])


class MaintenanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    booking_date: date
    workspace_id: int = Field(gt=0, le=MAX_CATALOG_ID)
    time_slot_ids: list[Annotated[int, Field(gt=0, le=MAX_CATALOG_ID)]] = Field(min_length=1, max_length=2)
    reason: str = Field(min_length=1, max_length=240)

    @field_validator('reason', mode='before')
    @classmethod
    def trim_reason(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('time_slot_ids')
    @classmethod
    def unique_slots(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Select each time slot only once.')
        return value


@router.get('/allocations')
def allocations(booking_date: date, session: Session = Depends(get_session)):
    return allocation_grid(session, booking_date)


@router.get('/roster')
def daily_roster(booking_date: date, session: Session = Depends(get_session)):
    return roster(session, booking_date)


@router.get('/members')
def member_directory(session: Session = Depends(get_session)):
    return members(session)


def action_result(session, booking_id, admin_id, action):
    booking = change_booking(session, booking_id, admin_id, action)
    row = serialize_booking(session, booking)
    row.update(booking_actions(session, booking))
    return row


@router.post('/bookings/{booking_id}/confirm', dependencies=[Depends(same_origin)])
def confirm(booking_id: str, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    return action_result(session, booking_id, admin.id, 'confirm')


@router.post('/bookings/{booking_id}/check-in', dependencies=[Depends(same_origin)])
def check_in(booking_id: str, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    return action_result(session, booking_id, admin.id, 'check-in')


@router.post('/bookings/{booking_id}/complete', dependencies=[Depends(same_origin)])
def complete(booking_id: str, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    return action_result(session, booking_id, admin.id, 'complete')


@router.post('/bookings/{booking_id}/cancel', dependencies=[Depends(same_origin)])
def cancel(booking_id: str, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    return action_result(session, booking_id, admin.id, 'cancel')


@router.post('/maintenance-blocks', status_code=201, dependencies=[Depends(same_origin)])
def maintenance(data: MaintenanceInput, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    return block_workspace(session, admin.id, data)


@router.delete('/maintenance-blocks/{block_id}', dependencies=[Depends(same_origin)])
def release_maintenance(block_id: str, session: Session = Depends(get_session)):
    return release_block(session, block_id)
