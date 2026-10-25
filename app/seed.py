"""Idempotent sample catalog for localhost; these are illustrative prices."""
from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, engine
from app.models import Package, TimeSlot, Workspace, WorkspaceType


def initialize(target=engine):
    Base.metadata.create_all(target)
    with Session(target) as session, session.begin():
        for name, kind, prefix, count in [('Shared desk', 'desk', 'DESK', 8), ('Private room', 'room', 'ROOM', 2)]:
            category = session.scalar(select(WorkspaceType).where(WorkspaceType.name == name))
            if category is None:
                category = WorkspaceType(name=name, kind=kind)
                session.add(category)
                session.flush()
            for index in range(1, count + 1):
                code = f'{prefix}-{index:02}'
                if session.scalar(select(Workspace.id).where(Workspace.code == code)) is None:
                    session.add(Workspace(code=code, workspace_type_id=category.id))
        for label, start, end in [('Morning', time(9), time(12)), ('Afternoon', time(13), time(16))]:
            if session.scalar(select(TimeSlot.id).where(TimeSlot.label == label)) is None:
                session.add(TimeSlot(label=label, starts_at=start, ends_at=end))
        for name, price, policy in [('3-Hour Pass', 15000, 'single'), ('Day Pass', 25000, 'day')]:
            if session.scalar(select(Package.id).where(Package.name == name)) is None:
                session.add(Package(name=name, price_minor=price, slot_policy=policy))


if __name__ == '__main__':
    initialize()
    print('Local database initialized. Existing catalog entries preserved.')
