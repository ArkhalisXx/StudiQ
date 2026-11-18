from datetime import date, datetime, time, timedelta
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.manage import set_admin
from app.migrations import MigrationError
from app.models import (AdminGrant, AuthSession, Booking, MaintenanceBlock, Package,
                        SlotAllocation, TimeSlot, User, Workspace, WorkspaceType)
from app.seed import initialize


def legacy_database(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    tables = [model.__table__ for model in [User, AuthSession, WorkspaceType, Workspace, TimeSlot, Package]]
    Base.metadata.create_all(target, tables=tables)
    with target.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE slot_allocations (
            id INTEGER NOT NULL PRIMARY KEY,
            booking_date DATE NOT NULL,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
            time_slot_id INTEGER NOT NULL REFERENCES time_slots(id),
            booking_id VARCHAR(36),
            maintenance_block_id VARCHAR(36),
            UNIQUE (booking_date, workspace_id, time_slot_id),
            CHECK ((booking_id IS NOT NULL) <> (maintenance_block_id IS NOT NULL))
        )''')
        connection.exec_driver_sql('CREATE INDEX ix_legacy_slot_date ON slot_allocations(booking_date)')
    with Session(target) as session, session.begin():
        session.add(User(id='existing-user', full_name='Existing Member', email='member@example.com',
                         password_hash='existing-password-hash', created_at=datetime(2026, 10, 25)))
        session.add(WorkspaceType(id=1, name='Shared desk', kind='desk'))
        session.flush()
        session.add(Workspace(id=1, workspace_type_id=1, code='DESK-01', is_active=False))
        session.add_all([
            TimeSlot(id=1, label='Morning', starts_at=time(9), ends_at=time(12)),
            TimeSlot(id=2, label='Afternoon', starts_at=time(13), ends_at=time(16)),
        ])
        session.add(Package(id=1, name='3-Hour Pass', price_minor=19900, slot_policy='single'))
        session.add(AuthSession(token_hash='a' * 64, user_id='existing-user',
                                expires_at=datetime.now() + timedelta(days=7)))
    return target


def assert_owner_foreign_keys(target):
    keys = {tuple(fk['constrained_columns']): fk['referred_table']
            for fk in inspect(target).get_foreign_keys('slot_allocations')}
    assert keys[('booking_id',)] == 'bookings'
    assert keys[('maintenance_block_id',)] == 'maintenance_blocks'


def test_legacy_upgrade_preserves_accounts_catalog_and_creates_backup(tmp_path):
    target = legacy_database(tmp_path)
    initialize(target)
    initialize(target)
    assert_owner_foreign_keys(target)
    with Session(target) as session:
        user = session.get(User, 'existing-user')
        assert user.full_name == 'Existing Member'
        assert user.password_hash == 'existing-password-hash'
        assert session.get(AuthSession, 'a' * 64).user_id == user.id
        assert session.get(Package, 1).price_minor == 19900
        assert session.get(Workspace, 1).is_active is False
        assert session.execute(text('SELECT version FROM schema_migrations')).scalars().all() == [1]
        assert session.scalar(select(AdminGrant)) is None
    assert 'ix_legacy_slot_date' in {index['name'] for index in inspect(target).get_indexes('slot_allocations')}
    backups = list((tmp_path / 'backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert backup.execute('SELECT password_hash FROM users').fetchone() == ('existing-password-hash',)
        owner_columns = {row[3] for row in backup.execute('PRAGMA foreign_key_list(slot_allocations)')}
        assert 'booking_id' not in owner_columns
    target.dispose()


@pytest.mark.parametrize('owner', ['booking', 'maintenance'])
def test_legacy_upgrade_keeps_allocations_with_valid_owners(tmp_path, owner):
    target = legacy_database(tmp_path)
    model = Booking if owner == 'booking' else MaintenanceBlock
    model.__table__.create(target)
    with Session(target) as session, session.begin():
        if owner == 'booking':
            session.add(Booking(id='retained-owner', customer_id='existing-user', requested_type_id=1,
                                package_id=1, workspace_id=1, booking_date=date(2026, 11, 20),
                                status='confirmed', price_minor=19900, package_name='3-Hour Pass',
                                slot_policy='single'))
        else:
            session.add(MaintenanceBlock(id='retained-owner', workspace_id=1, booking_date=date(2026, 11, 20),
                                         reason='Keep this maintenance record', created_by='existing-user'))
    with target.begin() as connection:
        field = 'booking_id' if owner == 'booking' else 'maintenance_block_id'
        connection.exec_driver_sql(
            f"INSERT INTO slot_allocations (id, booking_date, workspace_id, time_slot_id, {field}) "
            "VALUES (37, '2026-11-20', 1, 1, 'retained-owner')"
        )
    initialize(target)
    assert_owner_foreign_keys(target)
    with Session(target) as session:
        allocation = session.get(SlotAllocation, 37)
        assert allocation.workspace_id == 1
        assert allocation.time_slot_id == 1
        assert getattr(allocation, field) == 'retained-owner'
        assert session.get(model, 'retained-owner') is not None
    target.dispose()


@pytest.mark.parametrize('owner_column', ['booking_id', 'maintenance_block_id'])
def test_orphan_legacy_owner_aborts_upgrade_without_losing_rows(tmp_path, owner_column):
    target = legacy_database(tmp_path)
    with target.begin() as connection:
        connection.exec_driver_sql(
            f"INSERT INTO slot_allocations (booking_date, workspace_id, time_slot_id, {owner_column}) "
            "VALUES ('2026-11-20', 1, 1, 'unbacked-owner')"
        )
    with pytest.raises(MigrationError, match='missing'):
        initialize(target)
    with target.connect() as connection:
        assert connection.exec_driver_sql(f'SELECT {owner_column} FROM slot_allocations').scalar() == 'unbacked-owner'
        assert connection.exec_driver_sql('SELECT password_hash FROM users').scalar() == 'existing-password-hash'
    assert 'bookings' not in inspect(target).get_table_names()
    assert 'schema_migrations' not in inspect(target).get_table_names()
    assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1
    target.dispose()


def test_fresh_database_has_owner_foreign_keys_and_no_default_administrator(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "fresh.db"}')
    initialize(target)
    assert_owner_foreign_keys(target)
    with Session(target) as session:
        assert session.scalar(select(AdminGrant)) is None
        session.add(SlotAllocation(booking_date=date(2026, 11, 20), workspace_id=1,
                                   time_slot_id=1, booking_id='missing-booking'))
        with pytest.raises(IntegrityError):
            session.commit()
    assert not (tmp_path / 'backups').exists()
    target.dispose()


def test_newer_schema_version_is_not_modified(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "newer.db"}')
    initialize(target)
    with target.begin() as connection:
        connection.exec_driver_sql("INSERT INTO schema_migrations VALUES (2, 'future-version')")
    with pytest.raises(MigrationError, match='newer application'):
        initialize(target)
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar() == 2
    target.dispose()


def test_admin_grant_requires_existing_account_and_is_repeatable(tmp_path):
    target = legacy_database(tmp_path)
    set_admin(' MEMBER@EXAMPLE.COM ', True, target)
    with Session(target) as session:
        first_grant_time = session.get(AdminGrant, 'existing-user').created_at
    set_admin('member@example.com', True, target)
    with Session(target) as session:
        assert session.get(AdminGrant, 'existing-user').created_at == first_grant_time
    set_admin('member@example.com', False, target)
    set_admin('member@example.com', False, target)
    with Session(target) as session:
        assert session.get(AdminGrant, 'existing-user') is None
    with pytest.raises(ValueError, match='Register the account first'):
        set_admin('missing@example.com', True, target)
    with Session(target) as session:
        assert len(session.scalars(select(User)).all()) == 1
    target.dispose()


def test_grant_admin_command_uses_configured_database(tmp_path):
    target = legacy_database(tmp_path)
    target.dispose()
    environment = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "legacy.db"}'}
    result = subprocess.run([sys.executable, '-m', 'app.manage', 'grant-admin', 'MEMBER@example.com'],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'Administrator access granted.' in result.stdout
    with Session(target) as session:
        assert session.get(AdminGrant, 'existing-user') is not None
    target.dispose()
