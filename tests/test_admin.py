from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import auth
from app.api.catalog import local_today
from app.database import get_session, make_engine
from app.main import app
from app.models import (AdminGrant, Booking, MaintenanceBlock, MaintenanceSlot,
                        SlotAllocation, Workspace)
from app.seed import initialize
from app.services import admin, reservations

PASSWORD = 'a few memorable words 42'
ORIGIN = {'Origin': 'http://testserver'}
MANILA = ZoneInfo('Asia/Manila')


def register(client, email, name):
    response = client.post('/api/auth/register', json={
        'full_name': name, 'email': email, 'password': PASSWORD,
        'password_confirmation': PASSWORD,
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def operations(tmp_path, monkeypatch):
    engine = make_engine(f'sqlite:///{tmp_path / "operations.db"}')
    initialize(engine)
    monkeypatch.setattr(auth, 'limiter', auth.AuthRateLimiter())

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    customer = TestClient(app, headers=ORIGIN)
    try:
        with TestClient(app, headers=ORIGIN) as staff:
            staff_user = register(staff, 'staff@example.com', 'Test Staff')
            customer_user = register(customer, 'customer@example.com', 'Test Customer')
            with Session(engine) as session, session.begin():
                session.add(AdminGrant(user_id=staff_user['id'], created_at=admin.utc_now()))
            yield staff, customer, engine, staff_user, customer_user
    finally:
        customer.close()
        app.dependency_overrides.clear()
        engine.dispose()


def reserve(customer, package_id=1, **changes):
    data = {'booking_date': (local_today() + timedelta(days=1)).isoformat(),
            'workspace_type_id': 1, 'package_id': package_id}
    if package_id == 1:
        data['time_slot_id'] = 1
    data.update(changes)
    response = customer.post('/api/bookings', json=data)
    assert response.status_code == 201, response.text
    return response.json()


def action(staff, booking, name):
    return staff.post(f"/api/admin/bookings/{booking['id']}/{name}")


def freeze_time(monkeypatch, day, hour, minute=0):
    now = datetime.combine(day, time(hour, minute), MANILA).astimezone(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(admin, 'utc_now', lambda: now)
    monkeypatch.setattr(reservations, 'utc_now', lambda: now)
    return now


def block_data(day, workspace_id=1, slots=None, **changes):
    data = {'booking_date': day, 'workspace_id': workspace_id,
            'time_slot_ids': [1] if slots is None else slots, 'reason': 'Desk repair'}
    data.update(changes)
    return data


def test_administration_requires_a_trusted_grant(operations):
    staff, customer, _, _, _ = operations
    day = (local_today() + timedelta(days=1)).isoformat()
    for path in [f'/api/admin/allocations?booking_date={day}',
                 f'/api/admin/roster?booking_date={day}', '/api/admin/members']:
        assert customer.get(path).status_code == 403
        assert staff.get(path).status_code == 200
        with TestClient(app) as anonymous:
            assert anonymous.get(path).status_code == 401
    booking = reserve(customer)
    for name in ['confirm', 'check-in', 'complete', 'cancel']:
        assert action(customer, booking, name).status_code == 403
    assert customer.post('/api/admin/maintenance-blocks', json=block_data(day)).status_code == 403
    assert customer.delete('/api/admin/maintenance-blocks/missing').status_code == 403


def test_admin_confirmation_preserves_booking_snapshot_and_records_staff(operations):
    staff, customer, engine, staff_user, _ = operations
    booking = reserve(customer)
    response = action(staff, booking, 'confirm')
    assert response.status_code == 200, response.text
    row = response.json()
    assert row['status'] == 'confirmed'
    assert row['hold_expires_at'] is None
    assert row['price_minor'] == booking['price_minor']
    assert row['slots'] == booking['slots']
    assert row['can_confirm'] is False
    assert action(staff, booking, 'confirm').status_code == 200
    with Session(engine) as session:
        saved = session.get(Booking, booking['id'])
        assert saved.confirmed_by == staff_user['id']
        assert saved.confirmed_at is not None
        assert saved.hold_expires_at is None
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1
    cancelled = customer.post(f"/api/bookings/{booking['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()['status'] == 'cancelled'


def test_expired_hold_cannot_be_confirmed_and_is_released(operations):
    staff, customer, engine, _, _ = operations
    booking = reserve(customer)
    with Session(engine) as session, session.begin():
        session.get(Booking, booking['id']).hold_expires_at = reservations.utc_now() - timedelta(seconds=1)
    assert action(staff, booking, 'confirm').status_code == 409
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'expired'
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 0


def test_admin_rejects_invalid_transitions_and_unknown_booking(operations):
    staff, customer, _, _, _ = operations
    booking = reserve(customer)
    assert action(staff, booking, 'check-in').status_code == 409
    assert action(staff, booking, 'complete').status_code == 409
    assert action(staff, booking, 'confirm').status_code == 200
    assert action(staff, booking, 'check-in').status_code == 409
    assert action(staff, booking, 'complete').status_code == 409
    assert staff.post('/api/admin/bookings/missing/confirm').status_code == 404


def test_check_in_complete_roster_and_member_counts(operations, monkeypatch):
    staff, customer, engine, _, customer_user = operations
    booking = reserve(customer)
    assert action(staff, booking, 'confirm').status_code == 200
    day = datetime.fromisoformat(booking['booking_date']).date()
    freeze_time(monkeypatch, day, 9, 30)
    response = action(staff, booking, 'check-in')
    assert response.status_code == 200
    assert response.json()['status'] == 'checked_in'
    assert action(staff, booking, 'check-in').status_code == 200
    assert customer.post(f"/api/bookings/{booking['id']}/cancel").status_code == 409
    assert action(staff, booking, 'cancel').status_code == 409
    assert action(staff, booking, 'complete').json()['status'] == 'completed'
    assert action(staff, booking, 'complete').status_code == 200
    roster = staff.get('/api/admin/roster', params={'booking_date': booking['booking_date']}).json()
    row = next(row for row in roster if row['id'] == booking['id'])
    assert row['customer_name'] == customer_user['full_name']
    assert row['customer_email'] == customer_user['email']
    assert row['status'] == 'completed'
    members = staff.get('/api/admin/members').json()
    member = next(row for row in members if row['id'] == customer_user['id'])
    assert member['total_bookings'] == 1
    assert member['completed_bookings'] == 1
    assert 'password' not in str(members) and 'token' not in str(members)
    with Session(engine) as session:
        saved = session.get(Booking, booking['id'])
        assert saved.checked_in_at is not None
        assert saved.completed_at is not None


def test_day_pass_can_check_in_during_the_lunch_gap(operations, monkeypatch):
    staff, customer, _, _, _ = operations
    booking = reserve(customer, package_id=2)
    assert action(staff, booking, 'confirm').status_code == 200
    day = datetime.fromisoformat(booking['booking_date']).date()
    freeze_time(monkeypatch, day, 12, 30)
    response = action(staff, booking, 'check-in')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'checked_in'


def test_check_in_after_visit_and_confirmation_after_start_are_rejected(operations, monkeypatch):
    staff, customer, _, _, _ = operations
    confirmed = reserve(customer)
    assert action(staff, confirmed, 'confirm').status_code == 200
    pending = reserve(customer)
    day = datetime.fromisoformat(confirmed['booking_date']).date()
    freeze_time(monkeypatch, day, 12)
    assert action(staff, confirmed, 'check-in').status_code == 409
    assert action(staff, pending, 'confirm').status_code == 409
    assert customer.post(f"/api/bookings/{confirmed['id']}/cancel").status_code == 409


def test_checked_in_visits_are_completed_when_the_visit_ends(operations, monkeypatch):
    staff, customer, engine, _, _ = operations
    booking = reserve(customer)
    assert action(staff, booking, 'confirm').status_code == 200
    day = datetime.fromisoformat(booking['booking_date']).date()
    freeze_time(monkeypatch, day, 9, 30)
    assert action(staff, booking, 'check-in').status_code == 200
    freeze_time(monkeypatch, day, 12)
    roster = staff.get('/api/admin/roster', params={'booking_date': booking['booking_date']})
    assert roster.status_code == 200
    assert roster.json()[0]['status'] == 'completed'
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).completed_at is not None


def test_admin_cancel_is_idempotent_and_releases_inventory(operations):
    staff, customer, engine, _, _ = operations
    booking = reserve(customer)
    assert action(staff, booking, 'cancel').json()['status'] == 'cancelled'
    assert action(staff, booking, 'cancel').status_code == 200
    assert action(staff, booking, 'confirm').status_code == 409
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 0


def test_maintenance_reservation_conflicts_and_grid_are_consistent(operations):
    staff, customer, engine, _, _ = operations
    booking = reserve(customer)
    with Session(engine) as session:
        workspace_id = session.get(Booking, booking['id']).workspace_id
    data = block_data(booking['booking_date'], workspace_id)
    assert staff.post('/api/admin/maintenance-blocks', json=data).status_code == 409
    afternoon = staff.post('/api/admin/maintenance-blocks', json={**data, 'time_slot_ids': [2]})
    assert afternoon.status_code == 201, afternoon.text
    block = afternoon.json()
    assert staff.post('/api/admin/maintenance-blocks', json={**data, 'time_slot_ids': [2]}).status_code == 409
    grid = staff.get('/api/admin/allocations', params={'booking_date': booking['booking_date']}).json()
    workspace = next(row for row in grid['workspaces'] if row['id'] == workspace_id)
    cells = {cell['slot_id']: cell for cell in workspace['allocations']}
    assert cells[1]['status'] == 'pending_payment'
    assert cells[1]['booking_id'] == booking['id']
    assert cells[2]['status'] == 'maintenance'
    assert cells[2]['maintenance_block_id'] == block['id']
    assert cells[2]['reason'] == 'Desk repair'
    release = staff.delete(f"/api/admin/maintenance-blocks/{block['id']}")
    assert release.status_code == 200
    assert release.json()['released'] is True
    assert staff.delete(f"/api/admin/maintenance-blocks/{block['id']}").status_code == 200
    with Session(engine) as session:
        assert session.get(MaintenanceBlock, block['id']).released_at is not None
        assert session.scalar(select(func.count()).select_from(MaintenanceSlot)) == 1
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1


def test_maintenance_blocks_customer_booking_until_released(operations):
    staff, customer, engine, _, _ = operations
    with Session(engine) as session, session.begin():
        for workspace in session.scalars(select(Workspace).where(Workspace.workspace_type_id == 1)):
            workspace.is_active = workspace.id == 1
    day = (local_today() + timedelta(days=1)).isoformat()
    block = staff.post('/api/admin/maintenance-blocks', json=block_data(day, slots=[1, 2]))
    assert block.status_code == 201
    data = {'booking_date': day, 'workspace_type_id': 1, 'package_id': 2}
    assert customer.post('/api/bookings', json=data).status_code == 409
    assert staff.delete(f"/api/admin/maintenance-blocks/{block.json()['id']}").status_code == 200
    created = customer.post('/api/bookings', json=data)
    assert created.status_code == 201, created.text
    assert len(created.json()['slots']) == 2


@pytest.mark.parametrize('changes,status', [
    ({'booking_date': (local_today() - timedelta(days=1)).isoformat()}, 422),
    ({'booking_date': 'invalid'}, 422),
    ({'workspace_id': 999}, 404),
    ({'workspace_id': 2**63}, 422),
    ({'time_slot_ids': []}, 422),
    ({'time_slot_ids': [1, 1]}, 422),
    ({'time_slot_ids': [999]}, 422),
    ({'time_slot_ids': [2**63]}, 422),
    ({'reason': '   '}, 422),
    ({'reason': 'x' * 241}, 422),
    ({'created_by': 'someone-else'}, 422),
])
def test_invalid_maintenance_does_not_write_partial_records(operations, changes, status):
    staff, _, engine, _, _ = operations
    day = (local_today() + timedelta(days=1)).isoformat()
    response = staff.post('/api/admin/maintenance-blocks', json=block_data(day, **changes))
    assert response.status_code == status, response.text
    with Session(engine) as session:
        for model in [MaintenanceBlock, MaintenanceSlot, SlotAllocation]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_admin_mutations_require_same_origin(operations):
    staff, customer, engine, _, _ = operations
    booking = reserve(customer)
    data = block_data(booking['booking_date'], workspace_id=2)
    for origin in ['https://other.example', 'null', '']:
        headers = {'Origin': origin}
        for name in ['confirm', 'check-in', 'complete', 'cancel']:
            assert staff.post(f"/api/admin/bookings/{booking['id']}/{name}", headers=headers).status_code == 403
        assert staff.post('/api/admin/maintenance-blocks', json=data, headers=headers).status_code == 403
        assert staff.delete('/api/admin/maintenance-blocks/missing', headers=headers).status_code == 403
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.scalar(select(func.count()).select_from(MaintenanceBlock)) == 0
