from sqlalchemy import select

from app.models import SlotAllocation, Workspace


def available_count(session, booking_date, workspace_type_id, slot_ids):
    occupied = select(SlotAllocation.workspace_id).where(
        SlotAllocation.booking_date == booking_date,
        SlotAllocation.time_slot_id.in_(slot_ids),
    )
    return len(session.scalars(select(Workspace.id).where(
        Workspace.workspace_type_id == workspace_type_id,
        Workspace.is_active.is_(True),
        Workspace.id.not_in(occupied),
    )).all())
