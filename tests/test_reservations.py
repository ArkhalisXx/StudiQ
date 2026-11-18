from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import auth
from app.api.catalog import local_today
from app.database import get_session, make_engine
from app.main import app
from app.models import Booking, BookingSlot, Package, SlotAllocation, Workspace
from app.seed import initialize
from app.services import reservations

PASSWORD = 'a few memorable words 42'
ORIGIN = {'Origin': 'http://testserver'}
MANILA = ZoneInfo('Asia/Manila')


def register(client, email='member@example.com'):
    response = client.post('/api/auth/register', json={
        'full_name': 'Test Member', 'email': email,
        'password': PASSWORD, 'password_confirmation': PASSWORD,
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def reservations_app(tmp_path, monkeypatch):
    engine = make_engine(f'sqlite:///{tmp_path / "reservations.db"}')
    initialize(engine)
    monkeypatch.setattr(auth, 'limiter', auth.AuthRateLimiter())

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    browsers = []

    def new_browser(email):
        browser = TestClient(app, headers=ORIGIN)
        browsers.append(browser)
        register(browser, email)
        return browser

    try:
        with TestClient(app, headers=ORIGIN) as client:
            yield client, engine, new_browser
    finally:
        for browser in browsers:
            browser.close()
        app.dependency_overrides.clear()
        engine.dispose()


def reserve(client, **changes):
    data = {'booking_date': (local_today() + timedelta(days=1)).isoformat(),
            'workspace_type_id': 1, 'package_id': 1, 'time_slot_id': 1}
    data.update(changes)
    return client.post('/api/bookings', json=data)


def api_datetime(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)


def only_desks(engine, count):
    with Session(engine) as session, session.begin():
        desks = session.scalars(select(Workspace).where(Workspace.workspace_type_id == 1)
                                .order_by(Workspace.id)).all()
        for index, desk in enumerate(desks):
            desk.is_active = index < count
        return [desk.id for desk in desks[:count]]


def test_reservations_require_login(reservations_app):
    client, engine, _ = reservations_app
    assert reserve(client).status_code == 401
    assert client.get('/api/bookings/me').status_code == 401
    assert client.post('/api/bookings/not-owned/cancel').status_code == 401
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Booking)) == 0


def test_reservation_uses_catalog_price_and_keeps_a_snapshot(reservations_app):
    client, engine, _ = reservations_app
    user = register(client)
    created = reserve(client)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row['status'] == 'pending_payment'
    assert row['price_minor'] == 15000
    assert row['currency'] == 'PHP'
    assert row['slots'] == [{'id': 1, 'label': 'Morning', 'starts_at': '09:00', 'ends_at': '12:00'}]
    assert row['can_cancel'] is True
    availability = client.get('/api/workspaces/availability', params={
        'booking_date': row['booking_date'], 'workspace_type_id': 1,
        'package_id': 1, 'time_slot_id': 1,
    }).json()
    assert availability['available'] == 7
    assert availability['reservable'] is True
    hold_length = api_datetime(row['hold_expires_at']) - api_datetime(row['created_at'])
    assert timedelta(minutes=14, seconds=59) <= hold_length <= timedelta(minutes=15)
    with Session(engine) as session, session.begin():
        booking = session.get(Booking, row['id'])
        assert booking.customer_id == user['id']
        assert booking.price_minor == 15000
        assert session.scalar(select(func.count()).select_from(BookingSlot)) == 1
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1
        package = session.get(Package, 1)
        package.name = 'Changed package'
        package.price_minor = 99900
        package.slot_policy = 'day'
    saved = client.get('/api/bookings/me').json()[0]
    assert saved['id'] == row['id']
    assert saved['package_name'] == row['package_name']
    assert saved['price_minor'] == 15000
    assert saved['slots'] == row['slots']


def test_customer_history_and_cancellation_are_private_and_idempotent(reservations_app):
    client, engine, new_browser = reservations_app
    register(client)
    other = new_browser('other@example.com')
    booking = reserve(client).json()
    other_booking = reserve(other, time_slot_id=2).json()
    assert [row['id'] for row in client.get('/api/bookings/me').json()] == [booking['id']]
    assert [row['id'] for row in other.get('/api/bookings/me').json()] == [other_booking['id']]
    assert other.post(f"/api/bookings/{booking['id']}/cancel").status_code == 404
    assert client.get(f"/api/bookings/{booking['id']}").status_code == 200
    assert other.get(f"/api/bookings/{booking['id']}").status_code == 404
    assert client.post('/api/bookings/00000000-0000-0000-0000-000000000000/cancel').status_code == 404
    cancelled = client.post(f"/api/bookings/{booking['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()['status'] == 'cancelled'
    assert cancelled.json()['can_cancel'] is False
    assert client.post(f"/api/bookings/{booking['id']}/cancel").status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'cancelled'
        assert session.scalar(select(func.count()).select_from(SlotAllocation)
                              .where(SlotAllocation.booking_id == booking['id'])) == 0
        assert session.scalar(select(func.count()).select_from(BookingSlot)
                              .where(BookingSlot.booking_id == booking['id'])) == 1


def test_expired_hold_releases_inventory_and_cannot_be_cancelled(reservations_app):
    client, engine, new_browser = reservations_app
    register(client)
    other = new_browser('next@example.com')
    only_desks(engine, 1)
    first = reserve(client).json()
    with Session(engine) as session, session.begin():
        session.get(Booking, first['id']).hold_expires_at = reservations.utc_now() - timedelta(seconds=1)
    second = reserve(other)
    assert second.status_code == 201, second.text
    assert second.json()['workspace_code'] == first['workspace_code']
    assert client.get('/api/bookings/me').json()[0]['status'] == 'expired'
    assert client.post(f"/api/bookings/{first['id']}/cancel").status_code == 409
    with Session(engine) as session:
        assert session.get(Booking, first['id']).status == 'expired'
        allocations = session.scalars(select(SlotAllocation)).all()
        assert len(allocations) == 1
        assert allocations[0].booking_id == second.json()['id']


def test_day_pass_requires_one_workspace_for_both_slots(reservations_app):
    client, engine, _ = reservations_app
    register(client)
    desk_ids = only_desks(engine, 2)
    morning = reserve(client)
    assert morning.status_code == 201
    with Session(engine) as session, session.begin():
        session.get(Workspace, desk_ids[0]).is_active = False
    afternoon = reserve(client, time_slot_id=2)
    assert afternoon.status_code == 201
    with Session(engine) as session, session.begin():
        session.get(Workspace, desk_ids[0]).is_active = True
    assert morning.json()['workspace_code'] != afternoon.json()['workspace_code']
    day_request = {'booking_date': morning.json()['booking_date'], 'workspace_type_id': 1, 'package_id': 2}
    assert client.post('/api/bookings', json=day_request).status_code == 409
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Booking)) == 2
    assert client.post(f"/api/bookings/{morning.json()['id']}/cancel").status_code == 200
    day = client.post('/api/bookings', json=day_request)
    assert day.status_code == 201, day.text
    assert day.json()['workspace_code'] == morning.json()['workspace_code']
    assert len(day.json()['slots']) == 2
    with Session(engine) as session:
        allocations = session.scalars(select(SlotAllocation)
                                      .where(SlotAllocation.booking_id == day.json()['id'])).all()
        assert len(allocations) == 2
        assert len({allocation.workspace_id for allocation in allocations}) == 1


def test_simultaneous_customers_cannot_reserve_the_last_desk_twice(reservations_app):
    _, engine, new_browser = reservations_app
    first = new_browser('first@example.com')
    second = new_browser('second@example.com')
    only_desks(engine, 1)
    ready = Barrier(2)

    def request(client):
        ready.wait(timeout=5)
        return reserve(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, [first, second]))
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    availability = first.get('/api/workspaces/availability', params={
        'booking_date': winner['booking_date'], 'workspace_type_id': 1,
        'package_id': 1, 'time_slot_id': 1,
    }).json()
    assert availability['available'] == 0
    assert availability['reservable'] is False
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Booking)) == 1
        assert session.scalar(select(func.count()).select_from(BookingSlot)) == 1
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1


def test_booking_hold_ends_at_slot_start_and_started_slots_are_rejected(reservations_app, monkeypatch):
    client, _, _ = reservations_app
    register(client)
    day = local_today() + timedelta(days=1)
    now = datetime.combine(day, time(8, 55), MANILA).astimezone(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(reservations, 'utc_now', lambda: now)
    response = reserve(client, booking_date=day.isoformat())
    assert response.status_code == 201, response.text
    assert api_datetime(response.json()['hold_expires_at']) == now + timedelta(minutes=5)
    now += timedelta(minutes=5)
    assert reserve(client, booking_date=day.isoformat()).status_code == 422


@pytest.mark.parametrize('changes,status', [
    ({'booking_date': 'invalid'}, 422),
    ({'booking_date': (local_today() - timedelta(days=1)).isoformat()}, 422),
    ({'workspace_type_id': 999}, 404),
    ({'package_id': 999}, 404),
    ({'time_slot_id': None}, 422),
    ({'time_slot_id': 999}, 422),
    ({'workspace_type_id': 2**63}, 422),
    ({'package_id': 2**63}, 422),
    ({'time_slot_id': 2**63}, 422),
    ({'price_minor': 1}, 422),
    ({'status': 'confirmed'}, 422),
    ({'customer_id': 'someone-else'}, 422),
])
def test_invalid_reservations_do_not_write_partial_records(reservations_app, changes, status):
    client, engine, _ = reservations_app
    register(client)
    assert reserve(client, **changes).status_code == status
    with Session(engine) as session:
        for model in [Booking, BookingSlot, SlotAllocation]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_inactive_package_is_not_reservable(reservations_app):
    client, engine, _ = reservations_app
    register(client)
    with Session(engine) as session, session.begin():
        session.get(Package, 1).is_active = False
    assert reserve(client).status_code == 404


def test_reservation_mutations_require_same_origin(reservations_app):
    client, engine, _ = reservations_app
    register(client)
    booking = reserve(client).json()
    data = {'booking_date': booking['booking_date'], 'workspace_type_id': 1,
            'package_id': 1, 'time_slot_id': 2}
    for origin in ['https://other.example', 'null', '']:
        headers = {'Origin': origin}
        assert client.post('/api/bookings', json=data, headers=headers).status_code == 403
        assert client.post(f"/api/bookings/{booking['id']}/cancel", headers=headers).status_code == 403
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.scalar(select(func.count()).select_from(Booking)) == 1
