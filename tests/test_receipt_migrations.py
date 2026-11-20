from datetime import date, datetime, time, timezone
from uuid import UUID

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.migrations import MigrationError, SCHEMA_VERSION
from app.models import (ActiveCheckout, AdminGrant, AuthSession, Booking, BookingSlot,
                        EmailReceipt, Package, PaymentAttempt, PaymentEvent, SlotAllocation,
                        TimeSlot, User, Workspace, WorkspaceType)
from app.seed import initialize


def version_two_database(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "receipts.db"}')
    tables = [table for table in Base.metadata.sorted_tables
              if table.name not in {'email_receipts', 'menu_items'}]
    Base.metadata.create_all(target, tables=tables)
    with target.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE schema_migrations (
            version INTEGER NOT NULL PRIMARY KEY, applied_at TEXT NOT NULL
        )''')
        connection.exec_driver_sql("INSERT INTO schema_migrations VALUES (1, '2026-11-18T12:00:00Z')")
        connection.exec_driver_sql("INSERT INTO schema_migrations VALUES (2, '2026-11-20T12:00:00Z')")
    with Session(target) as session, session.begin():
        session.add(User(id='existing-user', full_name='Existing Member', email='member@example.com',
                         password_hash='existing-password-hash', created_at=datetime(2026, 11, 18)))
        session.add(WorkspaceType(id=1, name='Shared desk', kind='desk'))
        session.add(Package(id=1, name='3-Hour Pass', price_minor=19900, slot_policy='single'))
        session.add(TimeSlot(id=1, label='Morning', starts_at=time(9), ends_at=time(12)))
        session.flush()
        session.add(Workspace(id=1, workspace_type_id=1, code='DESK-01', is_active=False))
        session.flush()
        for identifier, status, day in [('paid-booking', 'confirmed', 25), ('pending-booking', 'pending_payment', 26)]:
            session.add(Booking(id=identifier, customer_id='existing-user', requested_type_id=1,
                                package_id=1, workspace_id=1, booking_date=date(2026, 11, day),
                                status=status, price_minor=15000, package_name='Original pass snapshot',
                                slot_policy='single', hold_expires_at=datetime(2026, 11, day, 8, 15)
                                if status == 'pending_payment' else None))
        session.flush()
        session.add(PaymentAttempt(id='paid-attempt', booking_id='paid-booking', status='paid',
                                  amount_minor=15000, provider_checkout_id='cs_paid', provider_payment_id='pay_paid',
                                  paid_at=datetime(2026, 11, 20, 12)))
        session.add(PaymentAttempt(id='pending-attempt', booking_id='pending-booking', status='pending',
                                  amount_minor=15000, provider_checkout_id='cs_pending'))
        session.flush()
        session.add_all([
            AdminGrant(user_id='existing-user', created_at=datetime(2026, 11, 18)),
            AuthSession(token_hash='a' * 64, user_id='existing-user', expires_at=datetime(2026, 12, 1)),
            ActiveCheckout(booking_id='pending-booking', attempt_id='pending-attempt'),
            PaymentEvent(provider_event_id='evt_paid', event_type='checkout_session.payment.paid',
                         attempt_id='paid-attempt', outcome='confirmed'),
            BookingSlot(booking_id='paid-booking', time_slot_id=1),
            SlotAllocation(id=37, booking_date=date(2026, 11, 25), workspace_id=1,
                           time_slot_id=1, booking_id='paid-booking'),
        ])
    return target


@pytest.fixture
def receipt_database(tmp_path):
    target = version_two_database(tmp_path)
    initialize(target)
    try:
        yield target
    finally:
        target.dispose()


def receipt_values(**changes):
    return {'attempt_id': 'paid-attempt', 'booking_id': 'paid-booking',
            'recipient': 'member@example.com', 'subject': 'Your StudiQ booking receipt',
            'text_body': 'Confirmed booking: Original pass snapshot, PHP 150.00.',
            'html_body': '<p>Confirmed booking: Original pass snapshot, PHP 150.00.</p>', **changes}


def test_receipt_upgrade_preserves_paid_data_and_active_checkout(tmp_path):
    target = version_two_database(tmp_path)
    before = {table: [column['name'] for column in inspect(target).get_columns(table)]
              for table in ['users', 'bookings', 'payment_attempts', 'payment_events', 'slot_allocations']}
    assert 'email_receipts' not in inspect(target).get_table_names()
    initialize(target)
    initialize(target)
    assert 'email_receipts' in inspect(target).get_table_names()
    assert {table: [column['name'] for column in inspect(target).get_columns(table)] for table in before} == before
    with Session(target) as session:
        assert session.get(User, 'existing-user').password_hash == 'existing-password-hash'
        assert session.get(AuthSession, 'a' * 64).expires_at == datetime(2026, 12, 1)
        assert session.get(AdminGrant, 'existing-user').created_at == datetime(2026, 11, 18)
        assert session.get(Booking, 'paid-booking').status == 'confirmed'
        assert session.get(Booking, 'paid-booking').price_minor == 15000
        assert session.get(Booking, 'pending-booking').status == 'pending_payment'
        assert session.get(BookingSlot, ('paid-booking', 1)) is not None
        assert session.get(SlotAllocation, 37).booking_id == 'paid-booking'
        assert session.get(Package, 1).price_minor == 19900
        assert session.get(Workspace, 1).is_active is False
        attempt = session.get(PaymentAttempt, 'paid-attempt')
        assert attempt.status == 'paid'
        assert attempt.provider_checkout_id == 'cs_paid'
        assert attempt.provider_payment_id == 'pay_paid'
        assert attempt.paid_at == datetime(2026, 11, 20, 12)
        assert session.get(ActiveCheckout, 'pending-booking').attempt_id == 'pending-attempt'
        assert session.get(PaymentEvent, 'evt_paid').outcome == 'confirmed'
        assert session.scalar(select(EmailReceipt)) is None
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version').scalars().all() == list(range(1, SCHEMA_VERSION + 1))
        assert connection.exec_driver_sql('SELECT applied_at FROM schema_migrations WHERE version = 2').scalar() == '2026-11-20T12:00:00Z'
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    assert not (tmp_path / 'backups').exists()
    target.dispose()


def test_receipt_snapshot_and_message_id_survive_retry_and_restart(receipt_database):
    with Session(receipt_database) as session, session.begin():
        receipt = EmailReceipt(**receipt_values())
        session.add(receipt)
        session.flush()
        receipt_id, message_id = receipt.id, receipt.message_id
        assert str(UUID(receipt_id)) == receipt_id
        assert message_id.startswith('<') and message_id.endswith('>')
        assert receipt.status == 'pending' and receipt.attempts == 0
        assert receipt.delivery_mode is None
        assert receipt.claimed_at is None and receipt.sent_at is None and receipt.last_error is None
        assert receipt.created_at.tzinfo is None and receipt.next_attempt_at.tzinfo is None
        assert receipt.next_attempt_at <= datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(receipt_database) as session, session.begin():
        receipt = session.get(EmailReceipt, receipt_id)
        receipt.status = 'pending'
        receipt.attempts = 1
        receipt.last_error = 'Email delivery failed. Please retry.'
        receipt.delivery_mode = 'smtp'
        session.get(User, 'existing-user').email = 'new-address@example.com'
        session.get(Booking, 'paid-booking').package_name = 'Changed booking label'
    initialize(receipt_database)
    with Session(receipt_database) as session:
        receipt = session.get(EmailReceipt, receipt_id)
        assert receipt.message_id == message_id
        assert receipt.recipient == 'member@example.com'
        assert receipt.subject == 'Your StudiQ booking receipt'
        assert receipt.text_body == receipt_values()['text_body']
        assert receipt.html_body == receipt_values()['html_body']
        assert receipt.attempts == 1 and receipt.delivery_mode == 'smtp'
        assert receipt.last_error == 'Email delivery failed. Please retry.'


def test_each_payment_attempt_has_at_most_one_receipt(receipt_database):
    with Session(receipt_database) as session, session.begin():
        session.add(EmailReceipt(**receipt_values()))
    with Session(receipt_database) as session:
        session.add(EmailReceipt(**receipt_values()))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert len(session.scalars(select(EmailReceipt)).all()) == 1


@pytest.mark.parametrize('changes', [
    {'status': 'delivered'}, {'attempts': -1}, {'delivery_mode': 'memory'}, {'attempt_id': None},
    {'attempt_id': 'missing-attempt'}, {'booking_id': 'missing-booking'},
])
def test_invalid_receipt_records_are_rejected(receipt_database, changes):
    with Session(receipt_database) as session:
        session.add(EmailReceipt(**receipt_values(**changes)))
        with pytest.raises(IntegrityError):
            session.commit()


def test_receipt_upgrade_failure_rolls_back_only_new_schema(tmp_path):
    target = version_two_database(tmp_path)
    with target.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        connection.exec_driver_sql("INSERT INTO auth_sessions VALUES ('broken-session', 'missing-user', '2026-12-01')")
        connection.commit()
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    with pytest.raises(MigrationError, match='rolled back'):
        initialize(target)
    assert 'email_receipts' not in inspect(target).get_table_names()
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version').scalars().all() == [1, 2]
        assert connection.exec_driver_sql("SELECT status FROM payment_attempts WHERE id='paid-attempt'").scalar() == 'paid'
        assert connection.exec_driver_sql("SELECT outcome FROM payment_events WHERE provider_event_id='evt_paid'").scalar() == 'confirmed'
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM slot_allocations').scalar() == 1
    target.dispose()


def test_fresh_database_includes_receipt_schema(tmp_path):
    target = make_engine(f'sqlite:///{tmp_path / "fresh.db"}')
    initialize(target)
    assert 'email_receipts' in inspect(target).get_table_names()
    with target.connect() as connection:
        assert connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version').scalars().all() == list(range(1, SCHEMA_VERSION + 1))
    target.dispose()
