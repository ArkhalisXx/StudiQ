from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import hmac
import json
from threading import Event, Lock
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api import auth
from app.api.catalog import local_today
from app.database import get_session, make_engine
from app.integrations.paymongo import PayMongoClient, PaymentProviderError
from app.main import app
from app.models import (ActiveCheckout, AdminGrant, Booking, Package, PaymentAttempt,
                        PaymentEvent, SlotAllocation, Workspace)
from app.seed import initialize
from app.services import payments
from app.services.auth import utc_now

PASSWORD = 'a few memorable words 42'
ORIGIN = {'Origin': 'http://testserver'}
WEBHOOK_SECRET = 'whsec_studiq_synthetic_test_secret'
SECRET_KEY = 'sk_test_studiq_synthetic_tests'


class FakeProvider:
    def __init__(self):
        self.created = []
        self.retrieved = []
        self.checkouts = {}
        self.create_error = None
        self.retrieve_error = None
        self.creation_started = Event()
        self.finish_creation = None
        self.lock = Lock()

    def create_checkout(self, booking, customer, attempt_id, success_url, cancel_url):
        with self.lock:
            self.created.append({'booking_id': booking.id,
                                 'amount': booking.price_minor, 'package_name': booking.package_name,
                                 'attempt_id': attempt_id, 'success_url': success_url,
                                 'cancel_url': cancel_url})
        self.creation_started.set()
        if self.finish_creation is not None:
            assert self.finish_creation.wait(timeout=10)
        if self.create_error is not None:
            raise self.create_error
        checkout_id = f'cs_{attempt_id}'
        self.checkouts[checkout_id] = {
            'id': checkout_id, 'checkout_url': f'https://checkout.paymongo.com/test/{checkout_id}',
            'status': 'active', 'livemode': False, 'reference_number': booking.id,
            'metadata': {'booking_id': booking.id, 'attempt_id': attempt_id},
            'payment_intent_id': f'pi_{attempt_id}', 'payments': [],
        }
        return {name: self.checkouts[checkout_id][name]
                for name in ['id', 'checkout_url', 'payment_intent_id']}

    def retrieve_checkout(self, checkout_id):
        self.retrieved.append(checkout_id)
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return deepcopy(self.checkouts[checkout_id])

    def mark_paid(self, attempt):
        checkout = self.checkouts[attempt.provider_checkout_id]
        checkout['status'] = 'paid'
        checkout['payments'] = [{
            'id': f'pay_{attempt.id}', 'status': 'paid', 'amount': attempt.amount_minor,
            'currency': 'PHP', 'livemode': False, 'paid_at': int(time.time()),
            'source_type': 'gcash',
        }]
        return checkout


def register(client, email='customer@example.com'):
    response = client.post('/api/auth/register', json={
        'full_name': 'Test Member', 'email': email,
        'password': PASSWORD, 'password_confirmation': PASSWORD,
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def payment_app(tmp_path, monkeypatch):
    engine = make_engine(f'sqlite:///{tmp_path / "payments.db"}')
    initialize(engine)
    monkeypatch.setattr(auth, 'limiter', auth.AuthRateLimiter())
    for name, value in {
        'PAYMENTS_ENABLED': 'true', 'PAYMONGO_MODE': 'test',
        'PAYMONGO_SECRET_KEY': SECRET_KEY, 'PAYMONGO_WEBHOOK_SECRET': WEBHOOK_SECRET,
        'STUDIQ_PUBLIC_URL': 'https://studiq.example',
    }.items():
        monkeypatch.setenv(name, value)
    provider = FakeProvider()
    monkeypatch.setattr(payments, 'get_provider', lambda settings: provider)

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    browsers = []

    def new_browser(email=None, admin=False, copy_cookies=None):
        browser = TestClient(app, headers=ORIGIN)
        browsers.append(browser)
        if copy_cookies is not None:
            browser.cookies.update(copy_cookies)
        elif email is not None:
            user = register(browser, email)
            if admin:
                with Session(engine) as session, session.begin():
                    session.add(AdminGrant(user_id=user['id'], created_at=utc_now()))
        return browser

    try:
        with TestClient(app, headers=ORIGIN) as customer:
            register(customer)
            webhook = TestClient(app)
            browsers.append(webhook)
            yield customer, webhook, engine, provider, new_browser
    finally:
        for browser in browsers:
            browser.close()
        app.dependency_overrides.clear()
        engine.dispose()


def reserve(customer):
    response = customer.post('/api/bookings', json={
        'booking_date': (local_today() + timedelta(days=1)).isoformat(),
        'workspace_type_id': 1, 'package_id': 1, 'time_slot_id': 1,
    })
    assert response.status_code == 201, response.text
    return response.json()


def checkout(customer, booking):
    response = customer.post(f"/api/checkout/{booking['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def saved_attempt(engine, attempt_id):
    with Session(engine) as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        session.expunge(attempt)
        return attempt


def payload(attempt, event_id='evt_paid_test', event_type='checkout_session.payment.paid'):
    return {'data': {'id': event_id, 'type': 'event', 'attributes': {
        'type': event_type, 'livemode': False, 'data': {
            'id': attempt.provider_checkout_id, 'type': 'checkout_session', 'attributes': {
                'metadata': {'booking_id': attempt.booking_id, 'attempt_id': attempt.id},
                'reference_number': attempt.booking_id,
            },
        },
    }}}


def signed_request(webhook, data, timestamp=None, signature=None):
    raw = json.dumps(data, separators=(',', ':')).encode()
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(WEBHOOK_SECRET.encode(), str(timestamp).encode() + b'.' + raw,
                      hashlib.sha256).hexdigest()
    signature = f't={timestamp},te={digest}' if signature is None else signature
    return webhook.post('/api/payments/webhook', content=raw, headers={
        'Content-Type': 'application/json', 'Paymongo-Signature': signature,
    })


def only_one_desk(engine):
    with Session(engine) as session, session.begin():
        for workspace in session.scalars(select(Workspace).where(Workspace.workspace_type_id == 1)):
            workspace.is_active = workspace.id == 1


def test_disabled_payments_do_not_call_the_provider(payment_app, monkeypatch):
    customer, _, engine, provider, _ = payment_app
    booking = reserve(customer)
    monkeypatch.setenv('PAYMENTS_ENABLED', 'false')
    assert customer.post(f"/api/checkout/{booking['id']}").status_code == 503
    config = customer.get('/api/payments/config')
    assert config.status_code == 200
    assert config.json()['enabled'] is False
    assert provider.created == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0


@pytest.mark.parametrize('mode,key', [
    ('test', 'sk_live_synthetic_tests'),
    ('live', SECRET_KEY),
    ('unexpected', SECRET_KEY),
    ('test', ''),
])
def test_invalid_payment_configuration_fails_before_provider_calls(payment_app, monkeypatch, mode, key):
    customer, _, engine, provider, _ = payment_app
    booking = reserve(customer)
    monkeypatch.setenv('PAYMONGO_MODE', mode)
    monkeypatch.setenv('PAYMONGO_SECRET_KEY', key)
    assert customer.post(f"/api/checkout/{booking['id']}").status_code == 503
    assert provider.created == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0


def test_checkout_requires_ownership_login_and_same_origin(payment_app):
    customer, webhook, engine, provider, new_browser = payment_app
    booking = reserve(customer)
    other = new_browser('other@example.com')
    url = f"/api/checkout/{booking['id']}"
    assert webhook.post(url, headers=ORIGIN).status_code == 401
    assert other.post(url).status_code == 404
    for origin in ['https://other.example', 'null', '']:
        assert customer.post(url, headers={'Origin': origin}).status_code == 403
    assert customer.post('/api/checkout/00000000-0000-0000-0000-000000000000').status_code == 404
    assert provider.created == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0


def test_checkout_uses_the_booking_snapshot_and_exposes_only_public_payment_data(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    with Session(engine) as session, session.begin():
        session.get(Package, 1).price_minor = 99900
    response = checkout(customer, booking)
    assert response['mode'] == 'test'
    assert response['checkout_url'].startswith('https://checkout.paymongo.com/')
    assert provider.created[0]['amount'] == 15000
    assert provider.created[0]['booking_id'] == booking['id']
    assert provider.created[0]['success_url'].startswith('https://studiq.example/')
    assert provider.created[0]['cancel_url'].startswith('https://studiq.example/')
    attempt = saved_attempt(engine, response['attempt_id'])
    assert attempt.amount_minor == 15000
    assert attempt.currency == 'PHP'
    assert attempt.mode == 'test'
    config = webhook.get('/api/payments/config')
    assert config.status_code == 200
    assert set(config.json()) == {'enabled', 'mode', 'provider', 'method'}
    assert config.json()['enabled'] is True and config.json()['mode'] == 'test'
    assert config.json()['provider'].casefold() == 'paymongo'
    assert config.json()['method'].casefold() == 'gcash'
    for public in [json.dumps(response), config.text, customer.get('/api/bookings/me').text]:
        assert SECRET_KEY not in public and WEBHOOK_SECRET not in public
        assert 'client_secret' not in public and 'password_hash' not in public
        assert 'customer@example.com' not in public


def test_repeated_checkout_reuses_one_attempt_without_extending_the_hold(payment_app):
    customer, _, engine, provider, _ = payment_app
    booking = reserve(customer)
    first = checkout(customer, booking)
    second = checkout(customer, booking)
    assert second['attempt_id'] == first['attempt_id']
    assert second['checkout_url'] == first['checkout_url']
    assert len(provider.created) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(ActiveCheckout)) == 1
        assert session.get(Booking, booking['id']).hold_expires_at.isoformat() == booking['hold_expires_at'].replace('Z', '')


def test_concurrent_checkout_requests_make_only_one_provider_session(payment_app):
    customer, _, engine, provider, new_browser = payment_app
    booking = reserve(customer)
    another_tab = new_browser(copy_cookies=customer.cookies)
    provider.finish_creation = Event()
    url = f"/api/checkout/{booking['id']}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(customer.post, url)
        assert provider.creation_started.wait(timeout=5)
        second = pool.submit(another_tab.post, url)
        try:
            assert second.result(timeout=5).status_code == 409
        finally:
            provider.finish_creation.set()
        assert first.result(timeout=5).status_code == 200
    assert len(provider.created) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(ActiveCheckout)) == 1


def test_checkout_creation_cannot_reopen_an_expired_and_reallocated_hold(payment_app):
    customer, webhook, engine, provider, new_browser = payment_app
    only_one_desk(engine)
    booking = reserve(customer)
    another_tab = new_browser(copy_cookies=customer.cookies)
    provider.finish_creation = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        creating = pool.submit(customer.post, f"/api/checkout/{booking['id']}")
        assert provider.creation_started.wait(timeout=5)
        try:
            with Session(engine) as session, session.begin():
                session.get(Booking, booking['id']).hold_expires_at = utc_now() - timedelta(seconds=1)
            assert another_tab.get('/api/bookings/me').status_code == 200
            replacement = reserve(new_browser('next@example.com'))
        finally:
            provider.finish_creation.set()
        response = creating.result(timeout=5)
    assert response.status_code == 409, response.text
    assert 'checkout_url' not in response.json()
    with Session(engine) as session:
        attempt_id = session.scalar(select(PaymentAttempt.id).where(PaymentAttempt.booking_id == booking['id']))
    attempt = saved_attempt(engine, attempt_id)
    assert attempt.status == 'expired'
    provider.mark_paid(attempt)
    assert signed_request(webhook, payload(attempt)).status_code == 200
    with Session(engine) as session:
        assert session.get(PaymentAttempt, attempt.id).status == 'review'
        assert session.get(Booking, booking['id']).status == 'expired'
        allocations = session.scalars(select(SlotAllocation)).all()
        assert len(allocations) == 1
        assert allocations[0].booking_id == replacement['id']


def test_uncertain_provider_creation_is_not_blindly_retried(payment_app):
    customer, _, engine, provider, _ = payment_app
    booking = reserve(customer)
    provider.create_error = PaymentProviderError(f'{SECRET_KEY} customer@example.com', uncertain=True)
    response = customer.post(f"/api/checkout/{booking['id']}")
    assert response.status_code == 502
    assert SECRET_KEY not in response.text and 'customer@example.com' not in response.text
    provider.create_error = None
    assert customer.post(f"/api/checkout/{booking['id']}").status_code == 409
    assert len(provider.created) == 1
    with Session(engine) as session:
        attempt = session.scalar(select(PaymentAttempt))
        assert attempt.status in {'creating', 'review'}
        assert session.get(ActiveCheckout, booking['id']).attempt_id == attempt.id


def test_definitive_provider_rejection_allows_a_new_attempt(payment_app):
    customer, _, engine, provider, _ = payment_app
    booking = reserve(customer)
    provider.create_error = PaymentProviderError('Synthetic rejected request', uncertain=False)
    assert customer.post(f"/api/checkout/{booking['id']}").status_code == 502
    with Session(engine) as session:
        assert session.scalar(select(PaymentAttempt)).status == 'failed'
        assert session.get(ActiveCheckout, booking['id']) is None
    provider.create_error = None
    assert checkout(customer, booking)['checkout_url']
    assert len(provider.created) == 2
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 2


def test_verified_paid_webhook_confirms_once_and_keeps_the_allocation(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    event = payload(attempt)
    response = signed_request(webhook, event)
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        saved = session.get(Booking, booking['id'])
        assert saved.status == 'confirmed'
        assert saved.hold_expires_at is None
        confirmed_at = saved.confirmed_at
        payment = session.get(PaymentAttempt, attempt.id)
        assert payment.status == 'paid'
        assert payment.provider_payment_id == f'pay_{attempt.id}'
        assert payment.paid_at is not None
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1
        assert session.scalar(select(func.count()).select_from(PaymentEvent)) == 1
    assert signed_request(webhook, event).status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).confirmed_at == confirmed_at
        assert session.scalar(select(func.count()).select_from(PaymentEvent)) == 1
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1


@pytest.mark.parametrize('case', ['bad_signature', 'missing_signature', 'old_timestamp', 'future_timestamp'])
def test_invalid_webhook_signatures_cannot_change_a_booking(payment_app, case):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    event = payload(attempt)
    if case == 'bad_signature':
        response = signed_request(webhook, event, signature=f't={int(time.time())},te=wrong')
    elif case == 'missing_signature':
        response = webhook.post('/api/payments/webhook', json=event)
    else:
        offset = -3600 if case == 'old_timestamp' else 3600
        response = signed_request(webhook, event, timestamp=int(time.time()) + offset)
    assert response.status_code == 400
    assert provider.retrieved == []
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.scalar(select(func.count()).select_from(PaymentEvent)) == 0


def test_oversized_webhook_is_rejected_before_provider_verification(payment_app):
    _, webhook, engine, provider, _ = payment_app
    response = webhook.post('/api/payments/webhook', content=b'x' * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert provider.retrieved == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PaymentEvent)) == 0


def test_signed_webhook_for_the_wrong_mode_is_rejected_before_provider_calls(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    event = payload(attempt)
    event['data']['attributes']['livemode'] = True
    assert signed_request(webhook, event).status_code == 400
    assert provider.retrieved == []
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.scalar(select(func.count()).select_from(PaymentEvent)) == 0


def test_checkout_return_pages_do_not_confirm_payment(payment_app):
    customer, _, engine, _, _ = payment_app
    booking = reserve(customer)
    checkout(customer, booking)
    for url in [f"/bookings?payment=success&booking_id={booking['id']}",
                f"/api/bookings/{booking['id']}?payment=success&status=paid"]:
        assert customer.get(url).status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.scalar(select(PaymentAttempt)).status == 'pending'


@pytest.mark.parametrize('field,value', [
    ('amount', 14999), ('currency', 'USD'), ('source_type', 'card'),
    ('payment_livemode', True), ('checkout_livemode', True),
    ('booking_metadata', 'another-booking'), ('attempt_metadata', 'another-attempt'),
    ('reference_number', 'another-booking'),
    ('payment_intent_id', 'pi_another_intent'),
])
def test_verified_payment_mismatches_require_review_without_confirmation(payment_app, field, value):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    canonical = provider.mark_paid(attempt)
    if field in {'amount', 'currency', 'source_type'}:
        canonical['payments'][0][field] = value
    elif field == 'payment_livemode':
        canonical['payments'][0]['livemode'] = value
    elif field == 'checkout_livemode':
        canonical['livemode'] = value
    elif field == 'booking_metadata':
        canonical['metadata']['booking_id'] = value
    elif field == 'attempt_metadata':
        canonical['metadata']['attempt_id'] = value
    else:
        canonical[field] = value
    response = signed_request(webhook, payload(attempt))
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.get(PaymentAttempt, attempt.id).status == 'review'
        assert session.get(PaymentAttempt, attempt.id).review_reason
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1


def test_wrong_provider_checkout_resource_cannot_confirm_or_poison_event_retry(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    canonical = provider.mark_paid(attempt)
    canonical['id'] = 'cs_another_checkout'
    event = payload(attempt)
    assert signed_request(webhook, event).status_code == 502
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'pending_payment'
        assert session.get(PaymentEvent, event['data']['id']) is None
    canonical['id'] = attempt.provider_checkout_id
    assert signed_request(webhook, event).status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'


@pytest.mark.parametrize('field', ['booking_id', 'attempt_id', 'reference_number'])
def test_signed_event_binding_mismatch_requires_review_even_when_provider_reports_paid(payment_app, field):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    event = payload(attempt)
    attributes = event['data']['attributes']['data']['attributes']
    if field == 'reference_number':
        attributes[field] = 'another-booking'
    else:
        attributes['metadata'][field] = 'another-value'
    response = signed_request(webhook, event)
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.get(PaymentAttempt, attempt.id).status == 'review'
        assert session.get(Booking, booking['id']).status == 'pending_payment'


@pytest.mark.parametrize('old_status', ['expired', 'cancelled'])
def test_late_paid_event_never_restores_a_reallocated_workspace(payment_app, old_status):
    customer, webhook, engine, provider, new_browser = payment_app
    only_one_desk(engine)
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    with Session(engine) as session, session.begin():
        saved = session.get(Booking, booking['id'])
        if old_status == 'expired':
            saved.hold_expires_at = utc_now() - timedelta(seconds=1)
        else:
            saved.status = 'cancelled'
            saved.cancelled_at = utc_now()
            saved.hold_expires_at = None
            session.execute(delete(SlotAllocation).where(SlotAllocation.booking_id == booking['id']))
    assert customer.get('/api/bookings/me').status_code == 200
    other = new_browser('next@example.com')
    replacement = reserve(other)
    assert replacement['workspace_code'] == booking['workspace_code']
    provider.mark_paid(attempt)
    response = signed_request(webhook, payload(attempt))
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == old_status
        assert session.get(PaymentAttempt, attempt.id).status == 'review'
        assert session.get(Booking, replacement['id']).status == 'pending_payment'
        allocations = session.scalars(select(SlotAllocation)).all()
        assert len(allocations) == 1
        assert allocations[0].booking_id == replacement['id']


def test_duplicate_and_out_of_order_events_cannot_downgrade_a_paid_booking(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    assert signed_request(webhook, payload(attempt)).status_code == 200
    for event_id, event_type in [
        ('evt_paid_again', 'checkout_session.payment.paid'),
        ('evt_late_failed', 'payment.failed'),
        ('evt_late_expired', 'checkout_session.expired'),
    ]:
        assert signed_request(webhook, payload(attempt, event_id, event_type)).status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        assert session.get(PaymentAttempt, attempt.id).status == 'paid'
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1


def test_provider_verification_timeout_can_retry_the_same_event_id(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    event = payload(attempt)
    provider.retrieve_error = PaymentProviderError('Synthetic timeout', uncertain=True)
    assert signed_request(webhook, event).status_code == 502
    with Session(engine) as session:
        assert session.get(PaymentEvent, event['data']['id']) is None
        assert session.get(Booking, booking['id']).status == 'pending_payment'
    provider.retrieve_error = None
    assert signed_request(webhook, event).status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        assert session.get(PaymentAttempt, attempt.id).status == 'paid'


def test_one_provider_payment_cannot_confirm_two_bookings(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    first_booking = reserve(customer)
    first_attempt = saved_attempt(engine, checkout(customer, first_booking)['attempt_id'])
    first_payment = provider.mark_paid(first_attempt)['payments'][0]
    assert signed_request(webhook, payload(first_attempt, 'evt_first')).status_code == 200
    second_booking = reserve(customer)
    second_attempt = saved_attempt(engine, checkout(customer, second_booking)['attempt_id'])
    provider.mark_paid(second_attempt)['payments'][0]['id'] = first_payment['id']
    response = signed_request(webhook, payload(second_attempt, 'evt_second'))
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.get(PaymentAttempt, first_attempt.id).status == 'paid'
        assert session.get(PaymentAttempt, second_attempt.id).status == 'review'
        assert session.get(Booking, first_booking['id']).status == 'confirmed'
        assert session.get(Booking, second_booking['id']).status == 'pending_payment'


def test_active_and_paid_checkout_block_cancellation_and_staff_confirmation(payment_app):
    customer, webhook, engine, provider, new_browser = payment_app
    staff = new_browser('staff@example.com', admin=True)
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    assert customer.post(f"/api/bookings/{booking['id']}/cancel").status_code == 409
    assert staff.post(f"/api/admin/bookings/{booking['id']}/cancel").status_code == 409
    assert staff.post(f"/api/admin/bookings/{booking['id']}/confirm").status_code == 409
    provider.mark_paid(attempt)
    assert signed_request(webhook, payload(attempt)).status_code == 200
    assert customer.post(f"/api/bookings/{booking['id']}/cancel").status_code == 409
    assert staff.post(f"/api/admin/bookings/{booking['id']}/cancel").status_code == 409
    own = customer.get('/api/bookings/me').json()[0]
    assert own['can_cancel'] is False
    roster = staff.get('/api/admin/roster', params={'booking_date': booking['booking_date']}).json()
    assert roster[0]['can_cancel'] is False
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1


def test_owner_reconciliation_verifies_provider_state_and_cannot_be_called_cross_site(payment_app):
    customer, _, engine, provider, new_browser = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    other = new_browser('other@example.com')
    url = f"/api/checkout/{booking['id']}/reconcile"
    assert other.post(url).status_code == 404
    assert customer.post(url, headers={'Origin': 'https://other.example'}).status_code == 403
    assert provider.retrieved == []
    provider.mark_paid(attempt)
    response = customer.post(url)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'confirmed'
    with Session(engine) as session:
        assert session.get(PaymentAttempt, attempt.id).status == 'paid'


@pytest.mark.parametrize('delivery', ['webhook', 'reconcile'])
def test_disabling_new_checkouts_still_allows_existing_payments_to_be_verified(payment_app, monkeypatch, delivery):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)
    monkeypatch.setenv('PAYMENTS_ENABLED', 'false')
    assert customer.get('/api/payments/config').json()['enabled'] is False
    if delivery == 'webhook':
        response = signed_request(webhook, payload(attempt))
    else:
        response = customer.post(f"/api/checkout/{booking['id']}/reconcile")
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        assert session.get(PaymentAttempt, attempt.id).status == 'paid'


def test_only_trusted_administrators_can_read_payment_reviews(payment_app):
    customer, webhook, engine, provider, new_browser = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    provider.mark_paid(attempt)['payments'][0]['amount'] = 14999
    assert signed_request(webhook, payload(attempt)).status_code == 200
    url = '/api/admin/payments/review'
    assert customer.get(url).status_code == 403
    assert webhook.get(url).status_code == 401
    staff = new_browser('staff@example.com', admin=True)
    response = staff.get(url)
    assert response.status_code == 200
    assert len(response.json()) == 1
    row = response.json()[0]
    assert row['attempt_id'] == attempt.id and row['booking_id'] == booking['id']
    assert row['review_reason']
    assert SECRET_KEY not in response.text and WEBHOOK_SECRET not in response.text
    assert 'client_secret' not in response.text and 'password_hash' not in response.text


def test_real_provider_adapter_creates_and_verifies_a_gcash_payment(payment_app, monkeypatch):
    customer, webhook, engine, _, _ = payment_app
    booking = reserve(customer)
    sent_attributes = {}
    requests = []
    checkout_id = 'cs_golden_integration'
    intent_id = 'pi_golden_integration'

    def provider_response(request):
        requests.append((request.method, request.url.path))
        assert request.url.host == 'api.paymongo.com'
        if request.method == 'POST' and request.url.path == '/v1/checkout_sessions':
            sent_attributes.update(json.loads(request.content)['data']['attributes'])
        else:
            assert request.method == 'GET'
            assert request.url.path == f'/v1/checkout_sessions/{checkout_id}'
        item = sent_attributes['line_items'][0]
        attributes = {
            'checkout_url': f'https://checkout.paymongo.com/test/{checkout_id}',
            'status': 'active', 'livemode': False,
            'reference_number': sent_attributes['reference_number'],
            'metadata': deepcopy(sent_attributes['metadata']),
            'payment_intent': {'id': intent_id, 'type': 'payment_intent',
                               'attributes': {'amount': item['amount'], 'currency': item['currency']}},
            'payments': [],
        }
        if request.method == 'GET':
            attributes['payments'] = [{
                'id': 'pay_golden_integration', 'type': 'payment', 'attributes': {
                    'status': 'paid', 'amount': item['amount'], 'currency': item['currency'],
                    'livemode': False, 'paid_at': int(time.time()),
                    'source': {'id': 'src_golden_integration',
                               'type': sent_attributes['payment_method_types'][0]},
                },
            }]
        return httpx.Response(200, json={'data': {
            'id': checkout_id, 'type': 'checkout_session', 'attributes': attributes,
        }})

    transport = httpx.MockTransport(provider_response)
    monkeypatch.setattr(payments, 'get_provider', lambda settings: PayMongoClient(
        settings['secret_key'], test_mode=settings['mode'] == 'test', transport=transport))
    result = checkout(customer, booking)
    attempt = saved_attempt(engine, result['attempt_id'])
    assert sent_attributes['reference_number'] == booking['id']
    assert sent_attributes['metadata'] == {'booking_id': booking['id'], 'attempt_id': attempt.id}
    assert sent_attributes['payment_method_types'] == ['gcash']
    assert sent_attributes['line_items'][0]['amount'] == booking['price_minor']
    assert sent_attributes['line_items'][0]['currency'] == 'PHP'
    assert booking['booking_date'] in sent_attributes['description']
    event = payload(attempt, 'evt_golden_integration')
    event['data']['attributes'].pop('livemode')
    event['data']['attributes']['data']['attributes'] = {
        'reference_number': sent_attributes['reference_number'],
        'metadata': deepcopy(sent_attributes['metadata']),
    }
    response = signed_request(webhook, event)
    assert response.status_code == 200, response.text
    assert requests == [('POST', '/v1/checkout_sessions'),
                        ('GET', f'/v1/checkout_sessions/{checkout_id}')]
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        payment = session.get(PaymentAttempt, attempt.id)
        assert payment.status == 'paid'
        assert payment.provider_payment_id == 'pay_golden_integration'
        assert payment.provider_payment_intent_id == intent_id
        assert session.scalar(select(func.count()).select_from(SlotAllocation)) == 1
        assert session.get(PaymentEvent, 'evt_golden_integration').outcome == 'paid'


def test_paid_event_retries_when_provider_has_not_yet_exposed_the_paid_record(payment_app):
    customer, webhook, engine, provider, _ = payment_app
    booking = reserve(customer)
    attempt = saved_attempt(engine, checkout(customer, booking)['attempt_id'])
    event = payload(attempt, 'evt_delayed_paid_record')
    assert signed_request(webhook, event).status_code == 502
    with Session(engine) as session:
        assert session.get(PaymentEvent, event['data']['id']) is None
        assert session.get(PaymentAttempt, attempt.id).status == 'pending'
        assert session.get(Booking, booking['id']).status == 'pending_payment'
    provider.mark_paid(attempt)
    assert signed_request(webhook, event).status_code == 200
    with Session(engine) as session:
        assert session.get(Booking, booking['id']).status == 'confirmed'
        assert session.get(PaymentAttempt, attempt.id).status == 'paid'
        assert session.get(PaymentEvent, event['data']['id']).outcome == 'paid'
