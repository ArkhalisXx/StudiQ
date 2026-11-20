import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.api import auth
from app.api.catalog import MAX_CATALOG_ID
from app.database import get_session, make_engine
from app.main import app
from app.migrations import SCHEMA_VERSION, upgrade
from app.models import AdminGrant, EmailReceipt, MenuItem
from app.seed import initialize
from app.services.auth import utc_now
from test_receipt_migrations import receipt_values, version_two_database

ORIGIN = {'Origin': 'http://testserver'}
PASSWORD = 'a few memorable words 42'


def item_data(**changes):
    return {'name': 'Iced coffee', 'description': 'Freshly brewed coffee over ice.',
            'category': 'drinks', 'price_minor': 9500,
            'is_available': True, 'is_active': True, **changes}


@pytest.fixture
def menu_app(tmp_path, monkeypatch):
    target = make_engine(f'sqlite:///{tmp_path / "menu.db"}')
    initialize(target)
    monkeypatch.setattr(auth, 'limiter', auth.AuthRateLimiter())

    def test_session():
        with Session(target) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    staff = TestClient(app, headers=ORIGIN)
    member = TestClient(app, headers=ORIGIN)
    guest = TestClient(app, headers=ORIGIN)
    try:
        for client, email in [(staff, 'staff@example.com'), (member, 'member@example.com')]:
            response = client.post('/api/auth/register', json={
                'full_name': 'Menu Member', 'email': email, 'password': PASSWORD,
                'password_confirmation': PASSWORD,
            })
            assert response.status_code == 201, response.text
            if client is staff:
                with Session(target) as session, session.begin():
                    session.add(AdminGrant(user_id=response.json()['id'], created_at=utc_now()))
        yield staff, member, guest, target
    finally:
        staff.close()
        member.close()
        guest.close()
        app.dependency_overrides.clear()
        target.dispose()


def create(staff, **changes):
    response = staff.post('/api/admin/menu', json=item_data(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def test_public_menu_shows_active_items_including_sold_out_items(menu_app):
    staff, member, guest, target = menu_app
    assert guest.get('/api/menu').json() == []
    create(staff, name='Cookie', category='snacks')
    create(staff, name='Tea', is_available=False)
    create(staff, name='Bowl', category='meals')
    create(staff, name='Coffee')
    hidden = create(staff, name='Hidden drink', is_active=False)

    public = guest.get('/api/menu')
    assert public.status_code == 200
    rows = public.json()
    assert [row['name'] for row in rows] == ['Coffee', 'Tea', 'Bowl', 'Cookie']
    assert rows[1]['is_available'] is False
    assert all(row['currency'] == 'PHP' for row in rows)
    assert all('is_active' not in row for row in rows)
    assert member.get('/api/menu').json() == rows
    assert [row['name'] for row in staff.get('/api/admin/menu').json()] == [
        'Bowl', 'Coffee', 'Cookie', 'Hidden drink', 'Tea']
    initialize(target)
    assert guest.get('/api/menu').json() == rows
    assert next(row for row in staff.get('/api/admin/menu').json()
                if row['id'] == hidden['id'])['is_active'] is False


def test_admin_menu_requires_a_grant_for_reads_and_writes(menu_app):
    staff, member, guest, target = menu_app
    item = create(staff)
    path = f"/api/admin/menu/{item['id']}"
    for client, status in [(guest, 401), (member, 403)]:
        assert client.get('/api/admin/menu').status_code == status
        assert client.post('/api/admin/menu', json=item_data(name='Tea')).status_code == status
        assert client.put(path, json=item_data(name='Changed')).status_code == status
        assert client.delete(path).status_code == status
    with Session(target) as session:
        assert session.scalar(select(func.count()).select_from(MenuItem)) == 1
        assert session.get(MenuItem, item['id']).name == 'Iced coffee'


def test_menu_create_update_and_permanent_delete_persist(menu_app):
    staff, _, guest, target = menu_app
    item = create(staff, name='  Coffee  ', description='  Fresh coffee\nServed over ice.  ')
    assert item['name'] == 'Coffee'
    assert item['description'] == 'Fresh coffee\nServed over ice.'
    path = f"/api/admin/menu/{item['id']}"
    changed = item_data(name='Rice bowl', description='<strong>Lunch</strong>',
                        category='meals', price_minor=14500, is_available=False, is_active=False)
    response = staff.put(path, json=changed)
    assert response.status_code == 200
    assert response.json() == {'id': item['id'], 'currency': 'PHP', **changed}
    assert guest.get('/api/menu').json() == []
    with Session(target) as session:
        saved = session.get(MenuItem, item['id'])
        assert saved.name == changed['name'] and saved.price_minor == 14500
        assert saved.is_available is False and saved.is_active is False
    assert staff.get('/api/admin/menu').json() == [response.json()]
    deleted = staff.delete(path)
    assert deleted.status_code == 204 and deleted.content == b''
    assert staff.get('/api/admin/menu').json() == []
    with Session(target) as session:
        assert session.get(MenuItem, item['id']) is None
    assert staff.put(path, json=changed).status_code == 404
    assert staff.delete(path).status_code == 404


def test_duplicate_names_are_case_insensitive_and_rollback_updates(menu_app):
    staff, _, _, target = menu_app
    first = create(staff)
    second = create(staff, name='Tea')
    duplicate = staff.post('/api/admin/menu', json=item_data(name='  ICED COFFEE  '))
    assert duplicate.status_code == 409
    response = staff.put(f"/api/admin/menu/{second['id']}", json=item_data(name='iced COFFEE'))
    assert response.status_code == 409
    assert 'INSERT' not in response.text and 'UNIQUE' not in response.text
    with Session(target) as session:
        assert session.get(MenuItem, first['id']).name == 'Iced coffee'
        assert session.get(MenuItem, second['id']).name == 'Tea'
        assert session.scalar(select(func.count()).select_from(MenuItem)) == 2
    assert create(staff, name='Juice')['name'] == 'Juice'


def test_invalid_menu_inputs_and_ids_do_not_change_records(menu_app):
    staff, _, _, target = menu_app
    item = create(staff)
    path = f"/api/admin/menu/{item['id']}"
    invalid = [
        {'name': ''}, {'name': '   '}, {'name': 'A' * 81}, {'name': '\nCoffee'},
        {'name': 'Coffee\tlarge'}, {'name': 'Coffee\x00'}, {'description': 'A' * 301},
        {'category': 'desserts'}, {'price_minor': -1}, {'price_minor': 1_000_001},
        {'price_minor': True}, {'price_minor': 9.5}, {'price_minor': 9500.0},
        {'price_minor': '9500'}, {'is_available': 'false'}, {'is_available': 0},
        {'is_active': 'true'}, {'is_active': 1}, {'currency': 'USD'}, {'id': 999},
    ]
    for changes in invalid:
        data = item_data(**changes)
        assert staff.post('/api/admin/menu', json=data).status_code == 422, changes
        assert staff.put(path, json=data).status_code == 422, changes
    for identifier in ['0', '-1', str(MAX_CATALOG_ID + 1), 'invalid']:
        assert staff.put(f'/api/admin/menu/{identifier}', json=item_data()).status_code == 422
        assert staff.delete(f'/api/admin/menu/{identifier}').status_code == 422
    assert staff.put(f'/api/admin/menu/{MAX_CATALOG_ID}', json=item_data()).status_code == 404
    assert staff.delete(f'/api/admin/menu/{MAX_CATALOG_ID}').status_code == 404
    with Session(target) as session:
        assert session.scalar(select(func.count()).select_from(MenuItem)) == 1
        assert session.get(MenuItem, item['id']).name == 'Iced coffee'


def test_menu_bounds_and_defaults_are_valid(menu_app):
    staff, _, guest, _ = menu_app
    response = staff.post('/api/admin/menu', json={
        'name': 'A' * 80, 'category': 'drinks', 'price_minor': 0,
    })
    assert response.status_code == 201
    assert response.json()['description'] == ''
    assert response.json()['is_active'] is True and response.json()['is_available'] is True
    maximum = create(staff, name='Large meal', category='meals', price_minor=1_000_000,
                     description='A' * 300)
    assert maximum['price_minor'] == 1_000_000 and maximum['currency'] == 'PHP'
    assert guest.get('/api/menu').json()[0]['price_minor'] == 0


def test_menu_writes_require_same_origin(menu_app):
    staff, _, _, target = menu_app
    item = create(staff)
    path = f"/api/admin/menu/{item['id']}"
    for origin in ['https://other.example', 'null', '']:
        headers = {'Origin': origin}
        assert staff.post('/api/admin/menu', json=item_data(name='Tea'), headers=headers).status_code == 403
        assert staff.put(path, json=item_data(name='Tea'), headers=headers).status_code == 403
        assert staff.delete(path, headers=headers).status_code == 403
    staff.headers.pop('origin')
    assert staff.post('/api/admin/menu', json=item_data(name='Tea')).status_code == 403
    assert staff.put(path, json=item_data(name='Tea')).status_code == 403
    assert staff.delete(path).status_code == 403
    assert staff.get('/api/admin/menu').status_code == 200
    with Session(target) as session:
        assert session.scalar(select(func.count()).select_from(MenuItem)) == 1
        assert session.get(MenuItem, item['id']).name == 'Iced coffee'


def test_version_three_menu_upgrade_preserves_existing_tables_and_records(tmp_path):
    target = version_two_database(tmp_path)
    EmailReceipt.__table__.create(target)
    with Session(target) as session, session.begin():
        session.add(EmailReceipt(**receipt_values(), id='existing-receipt',
                                 status='sent', attempts=1, delivery_mode='file',
                                 message_id='<original-receipt@example.com>', sent_at=utc_now()))
    with target.begin() as connection:
        connection.exec_driver_sql("INSERT INTO schema_migrations VALUES (3, '2026-11-20T13:00:00Z')")
    old_tables = inspect(target).get_table_names()
    assert 'menu_items' not in old_tables
    old_columns = {name: inspect(target).get_columns(name) for name in old_tables}
    with target.connect() as connection:
        old_rows = {name: connection.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY rowid').all()
                    for name in old_tables if name != 'schema_migrations'}
    upgrade(target)
    upgrade(target)
    assert set(inspect(target).get_table_names()) == set(old_tables) | {'menu_items'}
    for name, columns in old_columns.items():
        assert [str(column) for column in inspect(target).get_columns(name)] == [str(column) for column in columns]
    with target.connect() as connection:
        for name, rows in old_rows.items():
            assert connection.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY rowid').all() == rows
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM menu_items').scalar() == 0
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version').scalars().all() == list(range(1, SCHEMA_VERSION + 1))
        assert connection.exec_driver_sql('SELECT applied_at FROM schema_migrations WHERE version = 3').scalar() == '2026-11-20T13:00:00Z'
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    assert not (tmp_path / 'backups').exists()
    target.dispose()
