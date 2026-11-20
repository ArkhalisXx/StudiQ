from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from threading import Event

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.integrations import mail
from app.models import Booking, EmailReceipt, Package, PaymentAttempt, User
from app.seed import initialize
from app.services import receipts
from app.services.auth import utc_now
from test_payments import (checkout, payment_app, payload, reserve, saved_attempt, signed_request)


@pytest.fixture
def receipt_app(payment_app, monkeypatch, tmp_path):
    monkeypatch.setenv('RECEIPT_DELIVERY', 'file')
    monkeypatch.setenv('RECEIPT_FROM_EMAIL', '')
    monkeypatch.setattr(mail, 'INSTANCE', tmp_path)
    monkeypatch.setattr(receipts, 'INSTANCE', tmp_path)
    return payment_app


def paid_booking(receipt_app):
    customer, webhook, engine, provider, _ = receipt_app
    booking = reserve(customer)
    opened = checkout(customer, booking)
    attempt = saved_attempt(engine, opened['attempt_id'])
    provider.mark_paid(attempt)
    response = signed_request(webhook, payload(attempt))
    assert response.status_code == 200 and response.json()['status'] == 'paid', response.text
    return booking, attempt


def saved_receipt(engine):
    with Session(engine) as session:
        result = session.scalar(select(EmailReceipt))
        if result is not None:
            session.expunge(result)
        return result


def test_verified_payment_queues_one_receipt_and_ignores_duplicate_notifications(receipt_app):
    customer, webhook, engine, _, _ = receipt_app
    booking, attempt = paid_booking(receipt_app)
    receipt = saved_receipt(engine)
    assert receipt.status == 'pending' and receipt.attempts == 0
    assert receipt.recipient == 'customer@example.com'
    assert receipt.attempt_id == attempt.id and receipt.booking_id == booking['id']
    assert attempt.provider_payment_id is None  # Snapshot taken before provider confirmation.
    assert f'pay_{attempt.id}' in receipt.text_body
    assert 'TEST RECEIPT' in receipt.text_body and 'No real payment was charged.' in receipt.text_body
    assert booking['package_name'] in receipt.text_body
    assert signed_request(webhook, payload(attempt)).json()['status'] == 'duplicate'
    assert signed_request(webhook, payload(attempt, event_id='evt_another_delivery')).status_code == 200
    assert customer.post(f"/api/checkout/{booking['id']}/reconcile").status_code == 200
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EmailReceipt)) == 1
    assert saved_receipt(engine).message_id == receipt.message_id
    summary = customer.get('/api/bookings/me').json()[0]['payment']['receipt']
    assert summary['status'] == 'queued' and summary['can_retry'] is False


def test_receipt_preserves_payment_snapshot_after_catalog_and_account_changes(receipt_app):
    _, _, engine, _, _ = receipt_app
    booking, _ = paid_booking(receipt_app)
    receipt = saved_receipt(engine)
    with Session(engine) as session, session.begin():
        session.get(Package, 1).price_minor = 99000
        session.get(Booking, booking['id']).package_name = 'Renamed pass'
        session.scalar(select(User).where(User.email == 'customer@example.com')).email = 'changed@example.com'
    initialize(engine)
    assert receipts.process_receipts(engine) == 1
    result = saved_receipt(engine)
    assert result.recipient == receipt.recipient and result.text_body == receipt.text_body
    assert result.html_body == receipt.html_body and result.message_id == receipt.message_id


@pytest.mark.parametrize('outcome', ['pending', 'staff_confirmed', 'review'])
def test_no_receipt_without_a_verified_confirmed_payment(outcome, receipt_app):
    customer, webhook, engine, provider, browser = receipt_app
    booking = reserve(customer)
    if outcome == 'staff_confirmed':
        admin = browser('admin@example.com', admin=True)
        assert admin.post(f"/api/admin/bookings/{booking['id']}/confirm").status_code == 200
    elif outcome == 'review':
        opened = checkout(customer, booking)
        attempt = saved_attempt(engine, opened['attempt_id'])
        paid = provider.mark_paid(attempt)
        paid['payments'][0]['amount'] += 100
        assert signed_request(webhook, payload(attempt)).json()['status'] == 'review'
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 409
    assert saved_receipt(engine) is None
    assert customer.get('/api/bookings/me').json()[0]['payment']['receipt'] is None


def test_file_delivery_creates_a_private_download_and_does_not_claim_sent(receipt_app):
    customer, _, engine, _, browser = receipt_app
    booking, _ = paid_booking(receipt_app)
    assert receipts.process_receipts(engine) == 1
    receipt = saved_receipt(engine)
    assert receipt.status == 'preview' and receipt.sent_at is None
    assert receipts.process_receipts(engine) == 0
    summary = customer.get('/api/bookings/me').json()[0]['payment']['receipt']
    assert summary['status'] == 'preview' and summary['can_retry'] is False
    response = customer.get(summary['preview_url'])
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'].startswith('message/rfc822')
    assert 'attachment' in response.headers['content-disposition']
    email = BytesParser(policy=policy.default).parsebytes(response.content)
    assert email['Message-ID'] == receipt.message_id
    assert booking['id'] in email.get_body(preferencelist=('plain',)).get_content()
    assert browser('other@example.com').get(summary['preview_url']).status_code == 404
    assert browser().get(summary['preview_url']).status_code == 401


def test_receipt_post_requires_owner_and_same_origin(receipt_app):
    customer, _, engine, _, browser = receipt_app
    booking, _ = paid_booking(receipt_app)
    endpoint = f"/api/checkout/{booking['id']}/receipt"
    assert browser('other@example.com').post(endpoint).status_code == 404
    assert browser().post(endpoint).status_code == 401
    assert customer.post(endpoint, headers={'Origin': 'https://other.example'}).status_code == 403
    assert customer.post(endpoint, headers={'Origin': ''}).status_code == 403
    assert saved_receipt(engine).attempts == 0


@pytest.mark.parametrize('uncertain', [False, True])
def test_email_failure_keeps_booking_paid_and_requires_explicit_retry(uncertain, receipt_app, monkeypatch):
    customer, _, engine, _, _ = receipt_app
    booking, _ = paid_booking(receipt_app)

    def fail_delivery(receipt, settings):
        raise mail.MailDeliveryError('SMTP error includes a secret and address', uncertain=uncertain)

    monkeypatch.setattr(receipts, 'deliver_receipt', fail_delivery)
    assert receipts.process_receipts(engine) == 1
    receipt = saved_receipt(engine)
    assert receipt.status == 'failed' and receipt.attempts == 1
    assert 'secret' not in receipt.last_error and 'Check your inbox' in receipt.last_error
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        assert session.get(PaymentAttempt, receipt.attempt_id).status == 'paid'
    assert receipts.process_receipts(engine) == 0
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 409
    with Session(engine) as session, session.begin():
        session.get(EmailReceipt, receipt.id).next_attempt_at = utc_now() - timedelta(seconds=1)
    summary = customer.get('/api/bookings/me').json()[0]['payment']['receipt']
    assert summary['can_retry'] is True
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 200
    monkeypatch.setattr(receipts, 'deliver_receipt', lambda receipt, settings: 'sent')
    assert receipts.process_receipts(engine) == 1
    retried = saved_receipt(engine)
    assert retried.id == receipt.id and retried.message_id == receipt.message_id
    assert retried.status == 'sent' and retried.sent_at is not None and retried.attempts == 2
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 200
    assert receipts.process_receipts(engine) == 0


def test_two_workers_do_not_send_the_same_receipt_or_hold_database_during_send(receipt_app, monkeypatch):
    _, _, engine, _, _ = receipt_app
    paid_booking(receipt_app)
    started, finish = Event(), Event()
    calls = []

    def delivery(receipt, settings):
        calls.append(receipt.id)
        started.set()
        assert finish.wait(timeout=5)
        return 'sent'

    monkeypatch.setattr(receipts, 'deliver_receipt', delivery)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(receipts.process_receipts, engine)
        try:
            assert started.wait(timeout=5)
            second = workers.submit(receipts.process_receipts, engine)
            assert second.result(timeout=5) == 0
            with engine.begin() as connection:
                connection.exec_driver_sql('UPDATE email_receipts SET subject = subject')
        finally:
            finish.set()
        assert first.result(timeout=5) == 1
    assert len(calls) == 1 and saved_receipt(engine).status == 'sent'


def test_interrupted_delivery_is_not_automatically_sent_again(receipt_app):
    _, _, engine, _, _ = receipt_app
    paid_booking(receipt_app)
    receipt = saved_receipt(engine)
    with Session(engine) as session, session.begin():
        row = session.get(EmailReceipt, receipt.id)
        row.status = 'sending'
        row.claimed_at = utc_now() - timedelta(minutes=11)
        row.attempts = 1
    assert receipts.process_receipts(engine) == 0
    result = saved_receipt(engine)
    assert result.status == 'failed' and 'could not be confirmed' in result.last_error
    assert result.attempts == 1 and result.claimed_at is None


def test_old_paid_booking_can_prepare_missing_receipt(receipt_app):
    customer, _, engine, _, _ = receipt_app
    booking, _ = paid_booking(receipt_app)
    with Session(engine) as session, session.begin():
        session.execute(delete(EmailReceipt))
        session.get(Booking, booking['id']).status = 'completed'
    assert customer.get('/api/bookings/me').json()[0]['payment']['receipt']['status'] == 'not_requested'
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 200
    assert saved_receipt(engine) is not None


def test_local_preview_can_be_emailed_after_smtp_is_configured(receipt_app, monkeypatch):
    customer, _, engine, _, _ = receipt_app
    booking, _ = paid_booking(receipt_app)
    receipts.process_receipts(engine)
    preview = saved_receipt(engine)
    monkeypatch.setattr(receipts, 'get_mail_settings', lambda: {'mode': 'smtp', 'ready': True})
    assert customer.get('/api/bookings/me').json()[0]['payment']['receipt']['can_retry'] is True
    assert customer.post(f"/api/checkout/{booking['id']}/receipt").status_code == 200
    monkeypatch.setattr(receipts, 'deliver_receipt', lambda receipt, settings: 'sent')
    assert receipts.process_receipts(engine) == 1
    sent = saved_receipt(engine)
    assert sent.id == preview.id and sent.message_id == preview.message_id
    assert sent.status == 'sent' and sent.delivery_mode == 'smtp'
