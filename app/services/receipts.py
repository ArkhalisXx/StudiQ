"""Queue payment receipts and deliver them without holding a database lock."""
import logging
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import INSTANCE
from app.database import engine
from app.integrations.mail import MailDeliveryError, deliver_receipt, get_mail_settings, render_receipt
from app.models import Booking, EmailReceipt, PaymentAttempt, User, Workspace
from app.services.auth import utc_now
from app.services.reservations import begin_write, booking_slots, utc_string

RETRY_SECONDS = 60
STALE_MINUTES = 10
UNCERTAIN_MESSAGE = 'Delivery could not be confirmed. Check your inbox before retrying.'


def receipt_path(receipt_id):
    return INSTANCE / 'receipts' / f'{receipt_id}.eml'


def queue_receipt(session, attempt, booking):
    """Keep the rendered receipt in the same transaction as verified payment."""
    if attempt.status != 'paid' or booking.status not in {'confirmed', 'checked_in', 'completed'}:
        return None
    existing = session.scalar(select(EmailReceipt).where(EmailReceipt.attempt_id == attempt.id))
    if existing is not None:
        return existing
    customer = session.get(User, booking.customer_id)
    workspace = session.get(Workspace, booking.workspace_id)
    slots = booking_slots(session, booking.id)
    receipt_id = str(uuid4())
    content = render_receipt({
        'receipt_id': receipt_id, 'booking_id': booking.id,
        'payment_id': attempt.provider_payment_id, 'customer_name': customer.full_name,
        'package_name': booking.package_name, 'workspace_code': workspace.code,
        'booking_date': booking.booking_date,
        'slots': ' + '.join(f'{slot.label} {slot.starts_at:%H:%M}–{slot.ends_at:%H:%M}' for slot in slots),
        'amount_minor': attempt.amount_minor, 'currency': attempt.currency,
        'mode': attempt.mode, 'paid_at': attempt.paid_at,
    })
    receipt = EmailReceipt(id=receipt_id, attempt_id=attempt.id, booking_id=booking.id,
                           recipient=customer.email, subject=content['subject'],
                           text_body=content['text_body'], html_body=content['html_body'],
                           message_id=f'<receipt.{receipt_id}@studiq.local>')
    session.add(receipt)
    session.flush()
    return receipt


def summary_receipt(session, attempt):
    if attempt is None or attempt.status != 'paid':
        return None
    receipt = session.scalar(select(EmailReceipt).where(EmailReceipt.attempt_id == attempt.id))
    if receipt is None:
        return {'status': 'not_requested', 'can_retry': True, 'preview_url': None,
                'sent_at': None, 'message': None}
    preview_url = f'/api/receipts/{receipt.id}/preview' if receipt.status == 'preview' else None
    mail_settings = get_mail_settings()
    can_email_preview = (receipt.status == 'preview' and mail_settings['mode'] == 'smtp'
                         and mail_settings['ready'])
    return {'status': 'queued' if receipt.status == 'pending' else receipt.status,
            'can_retry': bool(can_email_preview or (receipt.status == 'failed' and receipt.next_attempt_at <= utc_now())),
            'preview_url': preview_url, 'sent_at': utc_string(receipt.sent_at),
            'message': receipt.last_error if receipt.status == 'failed' else None}


def request_receipt(session, booking_id, customer_id):
    with begin_write(session):
        booking = session.scalar(select(Booking).where(Booking.id == booking_id,
                                 Booking.customer_id == customer_id))
        if booking is None:
            raise HTTPException(404, 'Reservation not found.')
        attempt = session.scalar(select(PaymentAttempt).where(
            PaymentAttempt.booking_id == booking.id, PaymentAttempt.status == 'paid')
            .order_by(PaymentAttempt.created_at.desc()))
        if attempt is None:
            raise HTTPException(409, 'An email receipt is available only after a verified GCash payment.')
        receipt = queue_receipt(session, attempt, booking)
        if receipt is None:
            raise HTTPException(409, 'A receipt cannot be prepared for this reservation. Please contact staff.')
        if receipt.status == 'failed':
            if receipt.next_attempt_at > utc_now():
                raise HTTPException(409, 'Please wait a minute before retrying your email receipt.')
            receipt.status = 'pending'
            receipt.next_attempt_at = utc_now()
            receipt.claimed_at = None
            receipt.last_error = None
        elif receipt.status == 'preview':
            settings = get_mail_settings()
            if settings['mode'] == 'smtp' and settings['ready']:
                receipt.status = 'pending'
                receipt.next_attempt_at = utc_now()
    return booking


def preview_receipt(session, receipt_id, customer_id):
    receipt = session.scalar(select(EmailReceipt).join(Booking, Booking.id == EmailReceipt.booking_id)
                             .where(EmailReceipt.id == receipt_id, Booking.customer_id == customer_id,
                                    EmailReceipt.status == 'preview'))
    if receipt is None:
        raise HTTPException(404, 'Receipt preview not found.')
    path = receipt_path(receipt.id)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, 'Receipt preview is unavailable. Please contact staff.')
    return path


def claim_receipt(target):
    """A short transaction lets multiple workers safely share the outbox."""
    now = utc_now()
    with Session(target) as session, begin_write(session):
        stale = session.scalars(select(EmailReceipt).where(
            EmailReceipt.status == 'sending', EmailReceipt.claimed_at < now - timedelta(minutes=STALE_MINUTES))).all()
        for receipt in stale:
            # A crashed sender might already have handed the email to its server.
            receipt.status = 'failed'
            receipt.last_error = UNCERTAIN_MESSAGE
            receipt.next_attempt_at = now + timedelta(seconds=RETRY_SECONDS)
            receipt.claimed_at = None
        receipt = session.scalar(select(EmailReceipt).where(
            EmailReceipt.status == 'pending', EmailReceipt.next_attempt_at <= now)
            .order_by(EmailReceipt.created_at, EmailReceipt.id).limit(1).with_for_update())
        if receipt is None:
            return None
        settings = get_mail_settings()
        receipt.status = 'sending'
        receipt.claimed_at = now
        receipt.attempts += 1
        receipt.delivery_mode = settings['mode'] if settings['mode'] in {'file', 'smtp'} else None
        values = {name: getattr(receipt, name) for name in (
            'id', 'recipient', 'subject', 'text_body', 'html_body', 'message_id', 'attempts')}
    return SimpleNamespace(**values), settings


def process_receipts(target=engine, limit=10):
    processed = 0
    for _ in range(limit):
        claimed = claim_receipt(target)
        if claimed is None:
            break
        receipt, settings = claimed
        error_message = None
        try:
            if not settings.get('ready'):
                raise MailDeliveryError('Email delivery is not configured. Please contact staff.')
            outcome = deliver_receipt(receipt, settings)
            if outcome not in {'sent', 'preview'}:
                raise MailDeliveryError()
        except MailDeliveryError as error:
            outcome = 'failed'
            # Provider errors can include credentials or recipient data; store only our own wording.
            error_message = UNCERTAIN_MESSAGE if error.uncertain else 'Receipt delivery failed. Check your inbox before retrying.'
        except Exception:
            outcome = 'failed'
            error_message = UNCERTAIN_MESSAGE
        with Session(target) as session, begin_write(session):
            saved = session.get(EmailReceipt, receipt.id)
            if saved.status == 'sending' and saved.attempts == receipt.attempts:
                saved.status = outcome
                saved.claimed_at = None
                saved.last_error = error_message
                saved.next_attempt_at = utc_now() + timedelta(seconds=RETRY_SECONDS)
                saved.sent_at = utc_now() if outcome == 'sent' else None
        if error_message:
            logging.getLogger(__name__).warning('Receipt delivery needs attention for receipt %s.', receipt.id)
        processed += 1
    return processed
