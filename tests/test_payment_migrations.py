from datetime import date, datetime, time, timezone
from uuid import UUID

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.migrations import MigrationError, SCHEMA_VERSION
from app.models import (ActiveCheckout, AdminGrant, AuthSession, Booking, BookingSlot,
                        MaintenanceBlock, MaintenanceSlot, Package, PaymentAttempt,
                        PaymentEvent, SlotAllocation, TimeSlot, User, Workspace, WorkspaceType)
from app.seed import initialize

PAYMENT_TABLES = {'payment_attempts', 'active_checkouts', 'payment_events'}


def version_one_database(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "payments.db"}')
    old_tables = [table for table in Base.metadata.sorted_tables
                  if table.name not in PAYMENT_TABLES | {'email_receipts'}]
    Base.metadata.create_all(target, tables=old_tables)
    with target.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE schema_migrations (
            version INTEGER NOT NULL PRIMARY KEY, applied_at TEXT NOT NULL
        )''')
        connection.exec_driver_sql("INSERT INTO schema_migrations VALUES (1, '2026-11-19T12:00:00Z')")
    with Session(target) as session, session.begin():
        session.add(User(id='existing-user', full_name='Existing Member', email='member@example.com',
                         password_hash='existing-password-hash', created_at=datetime(2026, 11, 18)))
        session.add(WorkspaceType(id=1, name='Shared desk', kind='desk'))
        session.add(Package(id=1, name='3-Hour Pass', price_minor=19900, slot_policy='single'))
        session.add_all([
            TimeSlot(id=1, label='Morning', starts_at=time(9), ends_at=time(12)),
            TimeSlot(id=2, label='Afternoon', starts_at=time(13), ends_at=time(16)),
        ])
        session.flush()
        session.add(Workspace(id=1, workspace_type_id=1, code='DESK-01', is_active=False))
        session.flush()
        session.add(Booking(id='existing-booking', customer_id='existing-user', requested_type_id=1,
                            package_id=1, workspace_id=1, booking_date=date(2026, 11, 25),
                            status='confirmed', price_minor=15000, package_name='Original pass price',
                            slot_policy='single', confirmed_by='existing-user',
                            confirmed_at=datetime(2026, 11, 19, 12)))
        session.add(MaintenanceBlock(id='existing-maintenance', workspace_id=1, booking_date=date(2026, 11, 25),
                                     reason='Keep this maintenance record', created_by='existing-user'))
        session.flush()
        session.add_all([
            AdminGrant(user_id='existing-user', created_at=datetime(2026, 11, 18)),
            AuthSession(token_hash='a' * 64, user_id='existing-user', expires_at=datetime(2026, 12, 1)),
            BookingSlot(booking_id='existing-booking', time_slot_id=1),
            MaintenanceSlot(block_id='existing-maintenance', time_slot_id=2),
            SlotAllocation(id=37, booking_date=date(2026, 11, 25), workspace_id=1,
                           time_slot_id=1, booking_id='existing-booking'),
            SlotAllocation(id=38, booking_date=date(2026, 11, 25), workspace_id=1,
                           time_slot_id=2, maintenance_block_id='existing-maintenance'),
        ])
    return target


@pytest.fixture
def payment_database(tmp_path):
    target = version_one_database(tmp_path)
    initialize(target)
    try:
        yield target
    finally:
        target.dispose()


def test_payment_upgrade_preserves_version_one_data_and_repeats_safely(tmp_path):
    target = version_one_database(tmp_path)
    old_booking_columns = [column['name'] for column in inspect(target).get_columns('bookings')]
    assert PAYMENT_TABLES.isdisjoint(inspect(target).get_table_names())
    initialize(target)
    initialize(target)
    assert PAYMENT_TABLES.issubset(inspect(target).get_table_names())
    assert [column['name'] for column in inspect(target).get_columns('bookings')] == old_booking_columns
    with Session(target) as session:
        assert session.get(User, 'existing-user').password_hash == 'existing-password-hash'
        assert session.get(AuthSession, 'a' * 64).expires_at == datetime(2026, 12, 1)
        assert session.get(AdminGrant, 'existing-user').created_at == datetime(2026, 11, 18)
        booking = session.get(Booking, 'existing-booking')
        assert booking.status == 'confirmed'
        assert booking.price_minor == 15000
        assert booking.package_name == 'Original pass price'
        assert booking.confirmed_by == 'existing-user'
        assert session.get(BookingSlot, ('existing-booking', 1)) is not None
        assert session.get(MaintenanceBlock, 'existing-maintenance').reason == 'Keep this maintenance record'
        assert session.get(MaintenanceSlot, ('existing-maintenance', 2)) is not None
        assert session.get(SlotAllocation, 37).booking_id == 'existing-booking'
        assert session.get(SlotAllocation, 38).maintenance_block_id == 'existing-maintenance'
        assert session.get(Package, 1).price_minor == 19900
        assert session.get(Workspace, 1).is_active is False
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version').scalars().all() == list(range(1, SCHEMA_VERSION + 1))
        assert connection.exec_driver_sql('SELECT applied_at FROM schema_migrations WHERE version = 1').scalar() == '2026-11-19T12:00:00Z'
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    assert not (tmp_path / 'backups').exists()
    target.dispose()


def test_fresh_database_records_both_versions(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "fresh.db"}')
    initialize(target)
    assert PAYMENT_TABLES.issubset(inspect(target).get_table_names())
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version').scalars().all() == list(range(1, SCHEMA_VERSION + 1))
    target.dispose()


def test_repeated_initialization_keeps_payment_history_and_active_lock(payment_database):
    with Session(payment_database) as session, session.begin():
        attempt = PaymentAttempt(booking_id='existing-booking', amount_minor=15000,
                                 provider_checkout_id='cs_retained', provider_payment_intent_id='pi_retained')
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        session.add(ActiveCheckout(booking_id='existing-booking', attempt_id=attempt_id))
        session.add(PaymentEvent(provider_event_id='evt_retained', event_type='checkout_session.payment.paid',
                                 attempt_id=attempt_id, outcome='review'))
    initialize(payment_database)
    with Session(payment_database) as session:
        assert session.get(PaymentAttempt, attempt_id).provider_checkout_id == 'cs_retained'
        assert session.get(ActiveCheckout, 'existing-booking').attempt_id == attempt_id
        assert session.get(PaymentEvent, 'evt_retained').outcome == 'review'


@pytest.mark.parametrize('provider_field', ['provider_checkout_id', 'provider_payment_id'])
def test_provider_identifiers_cannot_be_reused(payment_database, provider_field):
    with Session(payment_database) as session, session.begin():
        session.add(PaymentAttempt(booking_id='existing-booking', amount_minor=15000,
                                  **{provider_field: 'provider_unique_id'}))
    with Session(payment_database) as session:
        session.add(PaymentAttempt(booking_id='existing-booking', amount_minor=15000,
                                  **{provider_field: 'provider_unique_id'}))
        with pytest.raises(IntegrityError):
            session.commit()


def test_only_one_checkout_can_be_active_for_a_booking(payment_database):
    with Session(payment_database) as session, session.begin():
        first = PaymentAttempt(booking_id='existing-booking', amount_minor=15000)
        second = PaymentAttempt(booking_id='existing-booking', amount_minor=15000)
        session.add_all([first, second])
        session.flush()
        first_id, second_id = first.id, second.id
        session.add(ActiveCheckout(booking_id='existing-booking', attempt_id=first_id))
    with Session(payment_database) as session:
        session.add(ActiveCheckout(booking_id='existing-booking', attempt_id=second_id))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.get(ActiveCheckout, 'existing-booking').attempt_id == first_id


def test_checkout_attempt_cannot_be_locked_twice(payment_database):
    with Session(payment_database) as session, session.begin():
        session.add(Booking(id='second-booking', customer_id='existing-user', requested_type_id=1,
                            package_id=1, workspace_id=1, booking_date=date(2026, 11, 26),
                            price_minor=15000, package_name='3-Hour Pass', slot_policy='single'))
        attempt = PaymentAttempt(booking_id='existing-booking', amount_minor=15000)
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        session.add(ActiveCheckout(booking_id='existing-booking', attempt_id=attempt_id))
    with Session(payment_database) as session:
        session.add(ActiveCheckout(booking_id='second-booking', attempt_id=attempt_id))
        with pytest.raises(IntegrityError):
            session.commit()


def test_provider_event_is_recorded_once(payment_database):
    with Session(payment_database) as session, session.begin():
        session.add(PaymentEvent(provider_event_id='evt_once', event_type='checkout_session.payment.paid', outcome='unknown_checkout'))
    with Session(payment_database) as session:
        session.add(PaymentEvent(provider_event_id='evt_once', event_type='different_event', outcome='paid'))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        event = session.get(PaymentEvent, 'evt_once')
        assert event.event_type == 'checkout_session.payment.paid'
        assert event.outcome == 'unknown_checkout'


@pytest.mark.parametrize('changes', [
    {'status': 'unknown-status'}, {'mode': 'production'}, {'amount_minor': -1}, {'currency': 'PESO'},
])
def test_invalid_payment_records_are_rejected(payment_database, changes):
    with Session(payment_database) as session:
        session.add(PaymentAttempt(**{'booking_id': 'existing-booking', 'amount_minor': 15000, **changes}))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize('record', [
    lambda: PaymentAttempt(booking_id='missing-booking', amount_minor=15000),
    lambda: ActiveCheckout(booking_id='existing-booking', attempt_id='missing-attempt'),
    lambda: PaymentEvent(provider_event_id='evt_missing', event_type='checkout_session.payment.paid', attempt_id='missing-attempt'),
])
def test_payment_foreign_keys_are_enforced(payment_database, record):
    with Session(payment_database) as session:
        session.add(record())
        with pytest.raises(IntegrityError):
            session.commit()


def test_new_attempt_has_safe_defaults_and_utc_timestamps(payment_database):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(payment_database) as session, session.begin():
        attempt = PaymentAttempt(booking_id='existing-booking', amount_minor=15000)
        session.add(attempt)
        session.flush()
        assert str(UUID(attempt.id)) == attempt.id
        assert attempt.provider == 'paymongo'
        assert attempt.status == 'creating'
        assert attempt.currency == 'PHP'
        assert attempt.mode == 'test'
        assert before <= attempt.created_at <= datetime.now(timezone.utc).replace(tzinfo=None)
        assert attempt.created_at.tzinfo is None and attempt.updated_at.tzinfo is None
        assert attempt.paid_at is None
        assert attempt.provider_checkout_id is None and attempt.provider_payment_id is None
        attempt_id, initial_update = attempt.id, attempt.updated_at
    with Session(payment_database) as session, session.begin():
        attempt = session.get(PaymentAttempt, attempt_id)
        attempt.status = 'pending'
        session.flush()
        assert attempt.updated_at >= initial_update


def test_failed_additive_upgrade_rolls_back_new_tables_and_version(tmp_path):
    target = version_one_database(tmp_path)
    with target.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        connection.exec_driver_sql("INSERT INTO auth_sessions VALUES ('bad-session', 'missing-user', '2026-12-01')")
        connection.commit()
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    with pytest.raises(MigrationError, match='rolled back'):
        initialize(target)
    assert PAYMENT_TABLES.isdisjoint(inspect(target).get_table_names())
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations').scalars().all() == [1]
        assert connection.exec_driver_sql("SELECT status FROM bookings WHERE id = 'existing-booking'").scalar() == 'confirmed'
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM slot_allocations').scalar() == 2
    target.dispose()
