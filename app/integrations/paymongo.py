"""PayMongo hosted GCash checkout and signed webhook verification.

API contract: PayMongo's official magento-extension/Gateway/PaymongoClient.php.
Signature format: official magento-extension/Controller/Payment/Webhook.php.
"""
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

API_BASE_URL = 'https://api.paymongo.com/v1'
CHECKOUT_HOST = 'checkout.paymongo.com'


class PaymentProviderError(Exception):
    def __init__(self, message='The payment provider is temporarily unavailable. Please try again later.', *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class WebhookSignatureError(Exception):
    pass


def trusted_checkout_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.hostname == CHECKOUT_HOST
                and url.port in (None, 443) and url.username is None and url.password is None)
    except ValueError:
        return False


def resource_id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r'[A-Za-z0-9_-]{1,180}', value) is not None


def payment_record(resource):
    if not isinstance(resource, dict) or not isinstance(resource.get('attributes'), dict):
        raise PaymentProviderError('The payment provider returned an unexpected response.')
    attributes = resource['attributes']
    source = attributes.get('source')
    return {
        'id': resource.get('id'), 'status': attributes.get('status'),
        'amount': attributes.get('amount'), 'currency': attributes.get('currency'),
        'livemode': attributes.get('livemode') if type(attributes.get('livemode')) is bool else None,
        'paid_at': attributes.get('paid_at'),
        'source_type': source.get('type') if isinstance(source, dict) else None,
    }


class PayMongoClient:
    def __init__(self, secret_key, test_mode=True, *, transport=None):
        if not isinstance(secret_key, str) or not secret_key:
            raise PaymentProviderError('Online payments are not configured.')
        self.secret_key = secret_key
        self.test_mode = test_mode
        self.transport = transport

    def _request(self, method, path, payload=None):
        creating = method == 'POST'
        try:
            with httpx.Client(auth=httpx.BasicAuth(self.secret_key, ''), timeout=20,
                              follow_redirects=False, verify=True, transport=self.transport) as client:
                response = client.request(method, API_BASE_URL + path, json=payload,
                                          headers={'Accept': 'application/json'})
        except httpx.RequestError:
            raise PaymentProviderError(uncertain=creating) from None
        if response.status_code >= 500:
            raise PaymentProviderError(uncertain=creating)
        if not 200 <= response.status_code < 300:
            raise PaymentProviderError('The payment provider could not complete this request.',
                                       uncertain=creating and not 400 <= response.status_code < 500)
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            raise PaymentProviderError('The payment provider returned an unexpected response.', uncertain=creating) from None
        if not isinstance(body, dict) or not isinstance(body.get('data'), dict):
            raise PaymentProviderError('The payment provider returned an unexpected response.', uncertain=creating)
        return body['data']

    def _checkout(self, resource, *, uncertain=False):
        attributes = resource.get('attributes')
        if (not resource_id(resource.get('id'), 'cs_') or resource.get('type') != 'checkout_session'
                or not isinstance(attributes, dict) or not trusted_checkout_url(attributes.get('checkout_url'))):
            raise PaymentProviderError('The payment provider returned an unexpected checkout response.', uncertain=uncertain)
        intent = attributes.get('payment_intent')
        intent_id = intent.get('id') if isinstance(intent, dict) else None
        if intent_id is not None and not resource_id(intent_id, 'pi_'):
            raise PaymentProviderError('The payment provider returned an unexpected checkout response.', uncertain=uncertain)
        payments = attributes.get('payments', [])
        if not isinstance(payments, list):
            raise PaymentProviderError('The payment provider returned an unexpected checkout response.', uncertain=uncertain)
        try:
            normalized_payments = [payment_record(payment) for payment in payments]
        except PaymentProviderError:
            raise PaymentProviderError('The payment provider returned an unexpected checkout response.', uncertain=uncertain) from None
        return {
            'id': resource['id'], 'checkout_url': attributes['checkout_url'],
            'status': attributes.get('status'),
            'livemode': attributes.get('livemode') if type(attributes.get('livemode')) is bool else None,
            'reference_number': attributes.get('reference_number'),
            'metadata': attributes.get('metadata') if isinstance(attributes.get('metadata'), dict) else {},
            'payment_intent_id': intent_id, 'payments': normalized_payments,
        }

    def create_checkout(self, booking, customer, attempt_id, success_url, cancel_url):
        if type(booking.price_minor) is not int or booking.price_minor <= 0:
            raise PaymentProviderError('This reservation cannot be paid online.')
        description = f'StudiQ {booking.package_name} · {booking.booking_date}'
        payload = {'data': {'attributes': {
            'line_items': [{'name': f'StudiQ {booking.package_name}', 'quantity': 1,
                            'amount': booking.price_minor, 'currency': 'PHP'}],
            'payment_method_types': ['gcash'], 'description': description,
            'send_email_receipt': False, 'show_description': True, 'show_line_items': True,
            'reference_number': str(booking.id), 'success_url': success_url, 'cancel_url': cancel_url,
            'metadata': {'booking_id': str(booking.id), 'attempt_id': str(attempt_id)},
        }}}
        resource = self._request('POST', '/checkout_sessions', payload)
        checkout = self._checkout(resource, uncertain=True)
        return {key: checkout[key] for key in ('id', 'checkout_url', 'payment_intent_id')}

    def retrieve_checkout(self, checkout_id):
        if not resource_id(checkout_id, 'cs_'):
            raise PaymentProviderError('Invalid checkout reference.')
        checkout = self._checkout(self._request('GET', '/checkout_sessions/' + checkout_id))
        if checkout['id'] != checkout_id:
            raise PaymentProviderError('The payment provider returned an unexpected checkout response.')
        if checkout['payment_intent_id'] and not any(p['status'] == 'paid' for p in checkout['payments']):
            intent = self._request('GET', '/payment_intents/' + checkout['payment_intent_id'])
            attributes = intent.get('attributes')
            if (intent.get('id') != checkout['payment_intent_id'] or intent.get('type') != 'payment_intent'
                    or not isinstance(attributes, dict) or not isinstance(attributes.get('payments', []), list)):
                raise PaymentProviderError('The payment provider returned an unexpected payment response.')
            checkout['payments'].extend(payment_record(payment) for payment in attributes.get('payments', []))
        return checkout

    def expire_checkout(self, checkout_id):
        if not resource_id(checkout_id, 'cs_'):
            raise PaymentProviderError('Invalid checkout reference.')
        resource = self._request('POST', '/checkout_sessions/' + checkout_id + '/expire')
        attributes = resource.get('attributes')
        if resource.get('id') != checkout_id or not isinstance(attributes, dict):
            raise PaymentProviderError('The payment provider returned an unexpected checkout response.', uncertain=True)
        return {'id': checkout_id, 'status': attributes.get('status')}


def verify_webhook(raw_body, signature, secret, test_mode=True, now=None):
    """Authenticate raw bytes before interpreting a PayMongo event."""
    error = 'The webhook signature is invalid.'
    if not isinstance(raw_body, bytes) or not isinstance(signature, str) or len(signature) > 4096:
        raise WebhookSignatureError(error)
    if not isinstance(secret, str) or not secret:
        raise WebhookSignatureError(error)
    fields = {}
    for component in signature.split(','):
        key, separator, value = component.strip().partition('=')
        if not separator or key not in {'t', 'te', 'li'} or key in fields:
            raise WebhookSignatureError(error)
        fields[key] = value
    timestamp = fields.get('t', '')
    signed = fields.get('te' if test_mode else 'li', '')
    if not re.fullmatch(r'[0-9]{1,14}', timestamp) or not re.fullmatch(r'[0-9a-fA-F]{64}', signed):
        raise WebhookSignatureError(error)
    if now is None:
        now = time.time()
    elif isinstance(now, datetime):
        now = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).timestamp()
    age = now - int(timestamp)
    if age > 300 or age < -60:
        raise WebhookSignatureError(error)
    try:
        expected = hmac.new(secret.encode('utf-8'), timestamp.encode('ascii') + b'.' + raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signed.lower()):
            raise WebhookSignatureError(error)
        event = json.loads(raw_body)
    except (UnicodeError, ValueError):
        raise WebhookSignatureError(error) from None
    if not isinstance(event, dict):
        raise WebhookSignatureError('The webhook payload is invalid.')
    return event
