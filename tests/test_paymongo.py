import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.paymongo import (PayMongoClient, PaymentProviderError,
                                      WebhookSignatureError, trusted_checkout_url, verify_webhook)

KEY = 'unit-test-api-key'
SIGNING_SECRET = 'unit-test-signing-secret'
NOW = 1800000000
BOOKING = SimpleNamespace(id='booking-id', price_minor=15000, package_name='3-Hour Pass', booking_date=date(2026, 11, 21))
CUSTOMER = SimpleNamespace(full_name='Test Member', email='member@example.com')


def payment(status='paid', **changes):
    attributes = {'status': status, 'amount': 15000, 'currency': 'PHP', 'livemode': False,
                  'paid_at': NOW, 'source': {'id': 'gcash_example', 'type': 'gcash'}}
    attributes.update(changes)
    return {'id': 'pay_example', 'type': 'payment', 'attributes': attributes}


def checkout(**changes):
    attributes = {'checkout_url': 'https://checkout.paymongo.com/cs_example', 'status': 'active',
                  'livemode': False, 'reference_number': 'booking-id',
                  'metadata': {'booking_id': 'booking-id', 'attempt_id': 'attempt-id'},
                  'payment_intent': {'id': 'pi_example', 'type': 'payment_intent'},
                  'payments': [payment()]}
    attributes.update(changes)
    return {'data': {'id': 'cs_example', 'type': 'checkout_session', 'attributes': attributes}}


def create(client):
    return client.create_checkout(BOOKING, CUSTOMER, 'attempt-id',
                                  'https://studiq.example/payment/success', 'https://studiq.example/payment/cancel')


def signature(body, *, timestamp=NOW, test_mode=True, secret=SIGNING_SECRET):
    mac = hmac.new(secret.encode(), str(timestamp).encode() + b'.' + body, hashlib.sha256).hexdigest()
    return f't={timestamp},te={mac if test_mode else ""},li={"" if test_mode else mac}'


def test_create_checkout_uses_server_price_php_gcash_and_fixed_api_origin():
    captured = []
    def handler(request):
        captured.append(request)
        assert str(request.url) == 'https://api.paymongo.com/v1/checkout_sessions'
        assert request.method == 'POST'
        expected_auth = base64.b64encode((KEY + ':').encode()).decode()
        assert request.headers['Authorization'] == 'Basic ' + expected_auth
        attributes = json.loads(request.content)['data']['attributes']
        assert attributes['payment_method_types'] == ['gcash']
        assert attributes['line_items'] == [{'name': 'StudiQ 3-Hour Pass', 'quantity': 1, 'amount': 15000, 'currency': 'PHP'}]
        assert attributes['metadata'] == {'booking_id': 'booking-id', 'attempt_id': 'attempt-id'}
        assert attributes['reference_number'] == 'booking-id'
        assert attributes['success_url'] == 'https://studiq.example/payment/success'
        assert attributes['cancel_url'] == 'https://studiq.example/payment/cancel'
        assert attributes['send_email_receipt'] is False
        return httpx.Response(200, json=checkout())
    result = create(PayMongoClient(KEY, transport=httpx.MockTransport(handler)))
    assert result == {'id': 'cs_example', 'checkout_url': 'https://checkout.paymongo.com/cs_example',
                      'payment_intent_id': 'pi_example'}
    assert len(captured) == 1


def test_retrieve_keeps_failed_and_paid_payments_without_assuming_first_was_paid():
    def handler(request):
        assert request.method == 'GET'
        return httpx.Response(200, json=checkout(payments=[payment('failed'), payment()]))
    result = PayMongoClient(KEY, transport=httpx.MockTransport(handler)).retrieve_checkout('cs_example')
    assert result['status'] == 'active'
    assert [p['status'] for p in result['payments']] == ['failed', 'paid']
    assert result['payments'][1] == {'id': 'pay_example', 'status': 'paid', 'amount': 15000, 'currency': 'PHP',
                                     'livemode': False, 'paid_at': NOW, 'source_type': 'gcash'}
    assert result['livemode'] is False
    assert result['metadata']['booking_id'] == BOOKING.id


def test_retrieve_can_resolve_payment_from_the_associated_intent():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == '/v1/checkout_sessions/cs_example':
            return httpx.Response(200, json=checkout(payments=[]))
        assert request.url.path == '/v1/payment_intents/pi_example'
        return httpx.Response(200, json={'data': {'id': 'pi_example', 'type': 'payment_intent',
                                                'attributes': {'status': 'succeeded', 'payments': [payment()]}}})
    result = PayMongoClient(KEY, transport=httpx.MockTransport(handler)).retrieve_checkout('cs_example')
    assert paths == ['/v1/checkout_sessions/cs_example', '/v1/payment_intents/pi_example']
    assert result['payments'][0]['status'] == 'paid'


def test_missing_mode_and_source_are_not_invented():
    raw_payment = payment()
    raw_payment['attributes'].pop('livemode')
    raw_payment['attributes'].pop('source')
    raw = checkout(payments=[raw_payment])
    raw['data']['attributes'].pop('livemode')
    client = PayMongoClient(KEY, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
    result = client.retrieve_checkout('cs_example')
    assert result['livemode'] is None
    assert result['payments'][0]['livemode'] is None
    assert result['payments'][0]['source_type'] is None


@pytest.mark.parametrize('status,uncertain', [(400, False), (401, False), (429, False), (500, True), (503, True)])
def test_provider_errors_are_safe_and_preserve_checkout_uncertainty(status, uncertain):
    client = PayMongoClient(KEY, transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text='private provider body: ' + KEY)))
    with pytest.raises(PaymentProviderError) as caught:
        create(client)
    assert caught.value.uncertain is uncertain
    assert KEY not in str(caught.value)
    assert 'private provider body' not in str(caught.value)


def test_timeout_does_not_retry_or_expose_authorization():
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout('Request timed out with secret ' + KEY, request=request)
    client = PayMongoClient(KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(PaymentProviderError) as caught:
        create(client)
    assert caught.value.uncertain is True
    assert len(requests) == 1
    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None


def test_redirects_do_not_forward_the_api_key_to_another_host():
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://attacker.example/collect'})
    with pytest.raises(PaymentProviderError) as caught:
        create(PayMongoClient(KEY, transport=httpx.MockTransport(handler)))
    assert requested == ['https://api.paymongo.com/v1/checkout_sessions']
    assert caught.value.uncertain is True


@pytest.mark.parametrize('bad_response', [None, [], {}, {'data': []}, {'data': {'id': 'cs_example'}},
    checkout(checkout_url='https://attacker.example/checkout'), checkout(payments=[None])])
def test_malformed_checkout_success_is_uncertain_and_not_a_redirect(bad_response):
    client = PayMongoClient(KEY, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=bad_response)))
    with pytest.raises(PaymentProviderError) as caught:
        create(client)
    assert caught.value.uncertain is True


@pytest.mark.parametrize('url', ['http://checkout.paymongo.com/cs_example',
    'https://checkout.paymongo.com.attacker.example/cs_example', 'https://attacker.example/checkout.paymongo.com',
    'https://user@checkout.paymongo.com/cs_example', 'https://checkout.paymongo.com:444/cs_example',
    'https://checkout.paymongo.com/cs_example\n', '//checkout.paymongo.com/cs_example', None])
def test_only_trusted_paymongo_https_checkout_urls_are_accepted(url):
    assert trusted_checkout_url(url) is False


@pytest.mark.parametrize('checkout_id', ['../payments', 'cs_example?extra=1', 'https://attacker.example', '', None])
def test_invalid_retrieve_identifiers_never_send_credentials(checkout_id):
    requested = []
    client = PayMongoClient(KEY, transport=httpx.MockTransport(lambda request: requested.append(request)))
    with pytest.raises(PaymentProviderError):
        client.retrieve_checkout(checkout_id)
    assert not requested


def test_expire_checkout_uses_the_official_endpoint():
    def handler(request):
        assert request.url.path == '/v1/checkout_sessions/cs_example/expire'
        assert request.method == 'POST'
        return httpx.Response(200, json=checkout(status='expired'))
    client = PayMongoClient(KEY, transport=httpx.MockTransport(handler))
    assert client.expire_checkout('cs_example') == {'id': 'cs_example', 'status': 'expired'}


@pytest.mark.parametrize('test_mode', [True, False])
def test_valid_signature_authenticates_exact_raw_bytes_in_the_configured_mode(test_mode):
    raw = b'{"data": {"id": "evt_example"}}\n'
    result = verify_webhook(raw, signature(raw, test_mode=test_mode), SIGNING_SECRET, test_mode=test_mode, now=NOW)
    assert result == {'data': {'id': 'evt_example'}}
    with pytest.raises(WebhookSignatureError):
        verify_webhook(raw.rstrip(), signature(raw, test_mode=test_mode), SIGNING_SECRET, test_mode=test_mode, now=NOW)


@pytest.mark.parametrize('offset,allowed', [(-300, True), (-301, False), (60, True), (61, False)])
def test_signature_timestamp_tolerance_rejects_old_and_future_replays(offset, allowed):
    raw = b'{}'
    header = signature(raw, timestamp=NOW + offset)
    if allowed:
        assert verify_webhook(raw, header, SIGNING_SECRET, now=NOW) == {}
    else:
        with pytest.raises(WebhookSignatureError):
            verify_webhook(raw, header, SIGNING_SECRET, now=NOW)


def test_signature_verifier_accepts_an_explicit_utc_datetime_clock():
    raw = b'{}'
    assert verify_webhook(raw, signature(raw), SIGNING_SECRET,
                          now=datetime.fromtimestamp(NOW, timezone.utc)) == {}


@pytest.mark.parametrize('header', [None, '', 't=bad,te=abcd,li=', 't=1800000000,te=abcd,li=',
    't=1800000000,t=1800000000,te=' + 'a'*64, 't=1800000000,version=1,te=' + 'a'*64])
def test_malformed_signature_headers_are_rejected(header):
    with pytest.raises(WebhookSignatureError):
        verify_webhook(b'{}', header, SIGNING_SECRET, now=NOW)


def test_a_valid_signature_from_the_other_mode_is_not_accepted():
    raw = b'{}'
    with pytest.raises(WebhookSignatureError):
        verify_webhook(raw, signature(raw, test_mode=False), SIGNING_SECRET, test_mode=True, now=NOW)
    with pytest.raises(WebhookSignatureError):
        verify_webhook(raw, signature(raw), SIGNING_SECRET, test_mode=False, now=NOW)


@pytest.mark.parametrize('raw', [b'invalid JSON', b'[]', b'"scalar"'])
def test_signed_invalid_payloads_are_rejected(raw):
    with pytest.raises(WebhookSignatureError):
        verify_webhook(raw, signature(raw), SIGNING_SECRET, now=NOW)
