from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.catalog import local_today
from app.database import get_session, make_engine
from app.main import app
from app.models import Package, SlotAllocation, TimeSlot, Workspace, WorkspaceType
from app.seed import initialize


@pytest.fixture
def baseline(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "test.db"}')
    initialize(engine)
    def session_override():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def query(client, **overrides):
    params = dict(booking_date=local_today().isoformat(), workspace_type_id=1, package_id=1, time_slot_id=1)
    params.update(overrides)
    return client.get('/api/workspaces/availability', params=params)


def test_home_catalog_and_assets(baseline):
    client, _ = baseline
    home = client.get('/')
    assert home.status_code == 200
    assert 'Find your space.' in home.text and '{{' not in home.text
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/static/style.css').status_code == 200
    assert client.get('/health').json() == {'status': 'ok', 'database': 'ok'}
    assert len(client.get('/api/packages').json()) == 2
    assert len(client.get('/api/time-slots').json()) == 2
    assert len(client.get('/api/workspace-types').json()) == 2
    assert query(client).json()['available'] == 8
    assert query(client).json()['reservable'] is False


def test_seed_is_repeatable_and_preserves_prices(baseline):
    _, engine = baseline
    with Session(engine) as session, session.begin():
        session.get(Package, 1).price_minor = 19900
    initialize(engine)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Workspace)) == 10
        assert session.get(Package, 1).price_minor == 19900


def test_day_pass_requires_same_workspace_and_hides_private_data(baseline):
    client, engine = baseline
    with Session(engine) as session, session.begin():
        # All but two desks are inactive; those two each have a different occupied slot.
        desks = session.scalars(select(Workspace).where(Workspace.workspace_type_id == 1).order_by(Workspace.id)).all()
        for desk in desks[2:]:
            desk.is_active = False
        session.add_all([
            SlotAllocation(booking_date=local_today(), workspace_id=desks[0].id, time_slot_id=1, booking_id='private-customer-id'),
            SlotAllocation(booking_date=local_today(), workspace_id=desks[1].id, time_slot_id=2, maintenance_block_id='private-maintenance-id'),
        ])
    assert query(client).json()['available'] == 1
    assert query(client, time_slot_id=2).json()['available'] == 1
    day = query(client, package_id=2)
    assert day.json()['available'] == 0
    assert day.json()['slots'] == ['Morning', 'Afternoon']
    assert 'private-' not in day.text


@pytest.mark.parametrize('changes,status', [
    ({'booking_date': 'bad'}, 422),
    ({'booking_date': (local_today() - timedelta(days=1)).isoformat()}, 422),
    ({'workspace_type_id': 999}, 404),
    ({'package_id': 999}, 404),
    ({'time_slot_id': 999}, 422),
    ({'time_slot_id': ''}, 422),
])
def test_invalid_availability(baseline, changes, status):
    assert query(baseline[0], **changes).status_code == status


@pytest.mark.parametrize('duplicate,both_owners', [(True, False), (False, True), (False, False)])
def test_allocation_constraints(baseline, duplicate, both_owners):
    _, engine = baseline
    with Session(engine) as session:
        if duplicate:
            session.add(SlotAllocation(booking_date=local_today(), workspace_id=1, time_slot_id=1, booking_id='first'))
            session.commit()
        session.add(SlotAllocation(booking_date=local_today(), workspace_id=1, time_slot_id=1,
                                  booking_id='second' if duplicate or both_owners else None,
                                  maintenance_block_id='maintenance' if both_owners else None))
        with pytest.raises(IntegrityError):
            session.commit()


def test_foreign_keys_enforced(baseline):
    with Session(baseline[1]) as session:
        session.add(SlotAllocation(booking_date=local_today(), workspace_id=999, time_slot_id=1, booking_id='invalid'))
        with pytest.raises(IntegrityError):
            session.commit()
