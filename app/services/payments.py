"""GCash checkout and payment confirmation through PayMongo."""
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.models import ActiveCheckout, Booking, PaymentAttempt, PaymentEvent, SlotAllocation, User
from app.services.auth import utc_now
from app.services.reservations import begin_write, booking_slots, expire_holds, slots_have_started


def get_settings():
    mode = os.getenv('PAYMONGO_MODE', 'test').strip().lower()
    key = os.getenv('PAYMONGO_SECRET_KEY', '').strip()
    secret = os.getenv('PAYMONGO_WEBHOOK_SECRET', '').strip()
    public_url = os.getenv('STUDIQ_PUBLIC_URL', '').strip().rstrip('/')
    enabled = os.getenv('PAYMENTS_ENABLED', 'false').strip().lower() == 'true'
    try:
        url = urlsplit(public_url)
        valid_url = (url.scheme == 'https' and bool(url.hostname) and not url.username
                     and not url.password and not url.query and not url.fragment and not url.path
                     and url.port in {None, 443})
    except ValueError:
        valid_url = False
    valid_mode = mode in {'test', 'live'} and key.startswith(f'sk_{mode}_')
    return {'enabled': enabled, 'mode': mode, 'secret_key': key, 'webhook_secret': secret,
            'public_url': public_url, 'configured': bool(valid_mode and secret),
            'ready': bool(enabled and valid_mode and secret and valid_url)}


def get_provider(settings):
    from app.integrations.paymongo import PayMongoClient
    return PayMongoClient(settings['secret_key'], test_mode=settings['mode'] == 'test')


def require_settings(allow_disabled=False):
    settings = get_settings()
    available = settings['configured'] if allow_disabled else settings['ready']
    if not available:
        raise HTTPException(503, 'GCash checkout is not configured. Please arrange payment with staff.')
    return settings


def checkout_lock(session, booking_id):
    return session.get(ActiveCheckout, booking_id)


def payment_blocks_changes(session, booking_id):
    if checkout_lock(session, booking_id):
        return True
    return session.scalar(select(PaymentAttempt.id).where(
        PaymentAttempt.booking_id == booking_id, PaymentAttempt.status.in_(['paid', 'review']))) is not None


def payment_summary(session, booking):
    settings = get_settings()
    lock = checkout_lock(session, booking.id)
    attempt = session.get(PaymentAttempt, lock.attempt_id) if lock else None
    if attempt is None:
        attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.booking_id == booking.id)
                                 .order_by(PaymentAttempt.created_at.desc(), PaymentAttempt.id.desc()))
    # A paid/review record must remain visible even if a later failed attempt exists.
    settled = session.scalar(select(PaymentAttempt).where(
        PaymentAttempt.booking_id == booking.id, PaymentAttempt.status.in_(['paid', 'review']))
        .order_by(PaymentAttempt.created_at.desc()))
    if settled is not None:
        attempt = settled
    status = attempt.status if attempt else 'unpaid'
    payable = (booking.status == 'pending_payment' and booking.hold_expires_at is not None
               and booking.hold_expires_at > utc_now()
               and not slots_have_started(booking.booking_date, booking_slots(session, booking.id)))
    can_pay = bool(settings['ready'] and payable and status not in {'paid', 'review', 'creating'}
                   and (not attempt or attempt.mode == settings['mode'])
                   and (not lock or status == 'pending'))
    paid_at = attempt.paid_at.isoformat() + 'Z' if attempt and attempt.paid_at else None
    return {'status': status, 'method': 'GCash', 'mode': attempt.mode if attempt else settings['mode'],
            'paid_at': paid_at, 'can_pay': can_pay, 'payments_enabled': settings['ready']}


def start_checkout(session, booking_id, customer_id):
    from app.integrations.paymongo import PaymentProviderError
    settings = require_settings()
    with begin_write(session):
        expire_holds(session)
        booking = session.scalar(select(Booking).where(Booking.id == booking_id,
                                 Booking.customer_id == customer_id).with_for_update())
        if booking is None:
            raise HTTPException(404, 'Reservation not found.')
        if (booking.status != 'pending_payment' or not booking.hold_expires_at
                or booking.hold_expires_at <= utc_now()
                or slots_have_started(booking.booking_date, booking_slots(session, booking.id))):
            raise HTTPException(409, 'This reservation can no longer start checkout.')
        recorded_payment = session.scalar(select(PaymentAttempt.id).where(
            PaymentAttempt.booking_id == booking.id, PaymentAttempt.status.in_(['paid', 'review'])))
        if recorded_payment:
            raise HTTPException(409, 'A payment is already recorded for this reservation. Please contact staff.')
        lock = checkout_lock(session, booking.id)
        if lock:
            attempt = session.get(PaymentAttempt, lock.attempt_id)
            if attempt.mode == settings['mode'] and attempt.status == 'pending' and attempt.checkout_url:
                return {'checkout_url': attempt.checkout_url, 'attempt_id': attempt.id, 'mode': attempt.mode}
            raise HTTPException(409, 'Checkout is already being processed. Refresh the payment status before trying again.')
        if payment_blocks_changes(session, booking.id):
            raise HTTPException(409, 'A payment is already recorded for this reservation. Please contact staff.')
        if booking.price_minor <= 0:
            raise HTTPException(422, 'This pass cannot be purchased through GCash.')
        customer = session.get(User, customer_id)
        attempt = PaymentAttempt(booking_id=booking.id, amount_minor=booking.price_minor,
                                 currency='PHP', mode=settings['mode'], status='creating')
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        session.add(ActiveCheckout(booking_id=booking.id, attempt_id=attempt_id))
        # Plain values survive the commit while the provider call runs outside the database lock.
        booking_name = booking.package_name
        booking_price = booking.price_minor
        booking_day = booking.booking_date
        customer_name, customer_email = customer.full_name, customer.email
    from types import SimpleNamespace
    provider_booking = SimpleNamespace(id=booking_id, package_name=booking_name, price_minor=booking_price,
                                       booking_date=booking_day)
    provider_customer = SimpleNamespace(full_name=customer_name, email=customer_email)
    success_url = settings['public_url'] + '/bookings?payment=success'
    cancel_url = settings['public_url'] + '/bookings?payment=cancelled'
    try:
        result = get_provider(settings).create_checkout(provider_booking, provider_customer,
                                                       attempt_id, success_url, cancel_url)
    except PaymentProviderError as error:
        with begin_write(session):
            attempt = session.get(PaymentAttempt, attempt_id)
            if attempt.status == 'creating':
                if not error.uncertain:
                    attempt.status = 'failed'
                    lock = checkout_lock(session, booking_id)
                    if lock and lock.attempt_id == attempt.id:
                        session.delete(lock)
                attempt.review_reason = ('Checkout creation could not be confirmed. Contact staff before retrying.'
                                         if error.uncertain else 'The provider rejected checkout creation.')
        raise HTTPException(502, 'GCash checkout could not be opened. Check your booking status before trying again.') from None
    with begin_write(session):
        attempt = session.get(PaymentAttempt, attempt_id)
        booking = session.get(Booking, booking_id)
        duplicate_checkout = session.scalar(select(PaymentAttempt.id).where(
            PaymentAttempt.provider_checkout_id == result['id'], PaymentAttempt.id != attempt_id))
        if duplicate_checkout:
            attempt.review_reason = 'The provider returned a checkout already assigned to another reservation.'
            raise HTTPException(502, 'The checkout reference could not be verified. Please contact staff.')
        if attempt.provider_checkout_id and attempt.provider_checkout_id != result['id']:
            attempt.status = 'review'
            attempt.review_reason = 'Conflicting checkout identifiers require staff review.'
        else:
            attempt.provider_checkout_id = result['id']
            attempt.checkout_url = result['checkout_url']
            attempt.provider_payment_intent_id = result.get('payment_intent_id')
            if attempt.status == 'creating':
                attempt.status = 'pending'
        expire_holds(session)
        still_payable = booking.status == 'pending_payment' and booking.hold_expires_at and booking.hold_expires_at > utc_now()
        if not still_payable and attempt.status == 'pending':
            attempt.status = 'expired'
        answer = {'checkout_url': attempt.checkout_url, 'attempt_id': attempt.id, 'mode': attempt.mode}
    if not still_payable or attempt.status in {'paid', 'review'}:
        raise HTTPException(409, 'Checkout status changed. Refresh My bookings before continuing.')
    return answer


def paid_time(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return utc_now()
        return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return utc_now()


def validate_paid_checkout(attempt, booking, checkout, payment, event_data=None):
    if checkout.get('id') != attempt.provider_checkout_id:
        return 'Checkout identifier does not match the reservation.'
    if checkout.get('livemode') is not (attempt.mode == 'live'):
        return 'Checkout mode does not match the payment attempt.'
    if checkout.get('reference_number') != booking.id:
        return 'Checkout reference does not match the reservation.'
    metadata = checkout.get('metadata')
    if not isinstance(metadata, dict) or metadata.get('booking_id') != booking.id or metadata.get('attempt_id') != attempt.id:
        return 'Checkout metadata does not match the reservation.'
    expected_intent = attempt.provider_payment_intent_id
    if expected_intent and checkout.get('payment_intent_id') != expected_intent:
        return 'Payment intent does not match the checkout.'
    if (type(payment.get('amount')) is not int or payment['amount'] != attempt.amount_minor
            or payment.get('currency') != attempt.currency):
        return 'The paid amount or currency does not match the pass price.'
    if payment.get('livemode') is not (attempt.mode == 'live'):
        return 'Payment mode does not match the payment attempt.'
    if payment.get('source_type') != 'gcash':
        return 'The payment method does not match GCash checkout.'
    if not isinstance(payment.get('id'), str) or not payment['id']:
        return 'The provider payment identifier is missing.'
    if event_data:
        attributes = event_data.get('attributes', {})
        event_meta = attributes.get('metadata')
        if event_meta is not None and event_meta != metadata:
            return 'Payment event metadata differs from the verified checkout.'
        reference = attributes.get('reference_number')
        if reference is not None and reference != booking.id:
            return 'Payment event reference differs from the verified checkout.'
    return None


def apply_checkout(session, attempt, checkout, event_data=None):
    """Apply a provider-retrieved checkout in the caller's inventory transaction."""
    booking = session.get(Booking, attempt.booking_id)
    if attempt.status in {'paid', 'review'}:
        return attempt.status
    paid = [p for p in checkout.get('payments', []) if p.get('status') == 'paid']
    if not paid:
        if checkout.get('status') in {'expired', 'cancelled'}:
            attempt.status = checkout['status']
            lock = checkout_lock(session, booking.id)
            if lock and lock.attempt_id == attempt.id:
                session.delete(lock)
        return attempt.status
    payment = paid[0]
    reason = validate_paid_checkout(attempt, booking, checkout, payment, event_data)
    if len(paid) != 1:
        reason = 'Multiple successful payments require staff review.'
    payment_id = payment.get('id')
    if isinstance(payment_id, str) and 1 <= len(payment_id) <= 100:
        other = session.scalar(select(PaymentAttempt).where(
            PaymentAttempt.provider_payment_id == payment_id, PaymentAttempt.id != attempt.id))
        if other is not None:
            reason = 'This provider payment is already associated with another attempt.'
        else:
            attempt.provider_payment_id = payment_id
    else:
        reason = 'The provider payment identifier is invalid.'
    attempt.paid_at = paid_time(payment.get('paid_at'))
    slots = booking_slots(session, booking.id)
    allocations = session.scalars(select(SlotAllocation).where(SlotAllocation.booking_id == booking.id)).all()
    expected_slots = {slot.id for slot in slots}
    held_slots = {a.time_slot_id for a in allocations
                  if a.workspace_id == booking.workspace_id and a.booking_date == booking.booking_date}
    lock = checkout_lock(session, booking.id)
    payable = (booking.status == 'pending_payment' and booking.hold_expires_at
               and booking.hold_expires_at > utc_now() and slots and not slots_have_started(booking.booking_date, slots)
               and expected_slots == held_slots and lock and lock.attempt_id == attempt.id)
    if not reason and not payable:
        reason = 'Payment arrived after the reservation changed or its hold expired. Refund or manual review is required.'
    if reason:
        attempt.status = 'review'
        attempt.review_reason = reason
    else:
        attempt.status = 'paid'
        attempt.review_reason = None
        booking.status = 'confirmed'
        booking.confirmed_at = utc_now()
        booking.confirmed_by = None
        booking.hold_expires_at = None
    if lock and lock.attempt_id == attempt.id:
        session.delete(lock)
    return attempt.status


def reconcile_checkout(session, booking_id, customer_id):
    from app.integrations.paymongo import PaymentProviderError
    settings = require_settings(allow_disabled=True)
    with begin_write(session):
        expire_holds(session)
        booking = session.scalar(select(Booking).where(Booking.id == booking_id, Booking.customer_id == customer_id))
        if booking is None:
            raise HTTPException(404, 'Reservation not found.')
        attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.booking_id == booking.id)
                                 .order_by(PaymentAttempt.created_at.desc()))
        if attempt is None:
            raise HTTPException(404, 'No GCash checkout was started for this reservation.')
        if attempt.status in {'paid', 'review'}:
            return booking
        if attempt.mode != settings['mode']:
            raise HTTPException(409, 'This checkout belongs to a different payment mode. Please contact staff.')
        if not attempt.provider_checkout_id:
            raise HTTPException(409, 'Checkout creation is still unconfirmed. Please contact staff before retrying.')
        attempt_id, checkout_id = attempt.id, attempt.provider_checkout_id
    try:
        checkout = get_provider(settings).retrieve_checkout(checkout_id)
    except PaymentProviderError:
        raise HTTPException(502, 'The payment provider could not be reached. Please refresh the status later.') from None
    with begin_write(session):
        expire_holds(session)
        attempt = session.get(PaymentAttempt, attempt_id)
        if checkout.get('id') != checkout_id:
            raise HTTPException(502, 'The provider returned an unexpected checkout. Please contact staff.')
        apply_checkout(session, attempt, checkout)
        booking = session.get(Booking, booking_id)
    return booking


def process_webhook(session, event):
    from app.integrations.paymongo import PaymentProviderError
    settings = require_settings(allow_disabled=True)
    try:
        data = event['data']
        event_id = data['id']
        attributes = data['attributes']
        event_type = attributes['type']
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100 or not isinstance(event_type, str) or len(event_type) > 100:
            raise ValueError()
        if 'livemode' in attributes and attributes['livemode'] is not (settings['mode'] == 'live'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, 'Invalid payment event.') from None
    with begin_write(session):
        if session.get(PaymentEvent, event_id):
            return {'status': 'duplicate'}
        if event_type != 'checkout_session.payment.paid':
            session.add(PaymentEvent(provider_event_id=event_id, event_type=event_type, outcome='ignored'))
            return {'status': 'ignored'}
    try:
        event_data = attributes['data']
        checkout_id = event_data['id']
        if not isinstance(event_data.get('attributes', {}), dict):
            raise ValueError()
        if not isinstance(checkout_id, str) or not checkout_id.startswith('cs_') or len(checkout_id) > 100:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, 'Invalid checkout event.') from None
    try:
        checkout = get_provider(settings).retrieve_checkout(checkout_id)
    except PaymentProviderError:
        # Do not record this event yet: a provider retry must be able to finish it.
        raise HTTPException(502, 'Payment verification is temporarily unavailable. Please retry this event.') from None
    if not any(payment.get('status') == 'paid' for payment in checkout.get('payments', [])):
        raise HTTPException(502, 'Payment details are not available yet. Please retry this event.')
    with begin_write(session):
        expire_holds(session)
        if session.get(PaymentEvent, event_id):
            return {'status': 'duplicate'}
        attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.provider_checkout_id == checkout_id))
        if attempt is None:
            metadata = checkout.get('metadata', {})
            attempt_id = metadata.get('attempt_id') if isinstance(metadata, dict) else None
            candidate = session.get(PaymentAttempt, attempt_id) if isinstance(attempt_id, str) else None
            if candidate and not candidate.provider_checkout_id:
                attempt = candidate
                attempt.provider_checkout_id = checkout_id
        if attempt is None:
            outcome = 'ignored'
        else:
            if checkout.get('id') != checkout_id:
                raise HTTPException(502, 'Payment checkout verification did not match. Please retry later.')
            outcome = apply_checkout(session, attempt, checkout, event_data)
        session.add(PaymentEvent(provider_event_id=event_id, event_type=event_type,
                                 attempt_id=attempt.id if attempt else None, outcome=outcome))
    return {'status': outcome}


def review_payments(session):
    records = session.scalars(select(PaymentAttempt).where(PaymentAttempt.status == 'review')
                              .order_by(PaymentAttempt.created_at.desc())).all()
    result = []
    for attempt in records:
        booking = session.get(Booking, attempt.booking_id)
        customer = session.get(User, booking.customer_id)
        result.append({'attempt_id': attempt.id, 'booking_id': booking.id, 'customer_name': customer.full_name,
                       'customer_email': customer.email, 'email': customer.email, 'amount_minor': attempt.amount_minor,
                       'currency': attempt.currency, 'mode': attempt.mode, 'review_reason': attempt.review_reason,
                       'created_at': attempt.created_at.isoformat()+'Z',
                       'paid_at': attempt.paid_at.isoformat()+'Z' if attempt.paid_at else None})
    return result
