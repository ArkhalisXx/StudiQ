"""Payment receipt rendering, private local previews, and encrypted SMTP delivery."""
import html
import os
import re
import smtplib
import ssl
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, parseaddr
from uuid import UUID
from zoneinfo import ZoneInfo

from email_validator import EmailNotValidError, validate_email

from app.config import INSTANCE


class MailDeliveryError(Exception):
    def __init__(self, message='Receipt delivery failed. Please try again later.', *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def valid_address(value):
    if not isinstance(value, str) or any(character in value for character in '\r\n'):
        return None
    try:
        _, address = parseaddr(value)
        return validate_email(address, check_deliverability=False).normalized
    except (EmailNotValidError, ValueError):
        return None


def get_mail_settings():
    mode = os.getenv('RECEIPT_DELIVERY', 'file').strip().lower()
    security = os.getenv('SMTP_SECURITY', 'starttls').strip().lower()
    try:
        port = int(os.getenv('SMTP_PORT', '465' if security == 'ssl' else '587'))
        valid_port = 1 <= port <= 65535
    except ValueError:
        port, valid_port = None, False
    host = os.getenv('SMTP_HOST', '').strip()
    username = os.getenv('SMTP_USERNAME', '')
    password = os.getenv('SMTP_PASSWORD', '')
    sender = os.getenv('RECEIPT_FROM_EMAIL', '').strip()
    if not sender and mode == 'file':
        sender = 'receipts@example.com'
    from_email = valid_address(sender)
    from_name = os.getenv('RECEIPT_FROM_NAME', 'StudiQ').strip()
    valid_name = bool(from_name) and not any(ord(character) < 32 or ord(character) == 127 for character in from_name)
    valid_host = bool(host) and not any(character.isspace() or character in '/\\@' for character in host)
    ready = mode in {'file', 'smtp'} and bool(from_email) and valid_name
    if mode == 'smtp':
        ready = ready and security in {'starttls', 'ssl'} and valid_host and valid_port and bool(username) == bool(password)
    return {'mode': mode, 'ready': bool(ready), 'smtp_host': host, 'smtp_port': port,
            'smtp_username': username, 'smtp_password': password, 'smtp_security': security,
            'from_email': from_email, 'from_name': from_name}


def build_message(receipt, settings):
    recipient = valid_address(receipt.recipient)
    sender = valid_address(settings.get('from_email'))
    message_id = receipt.message_id
    if (not recipient or not sender or not isinstance(message_id, str)
            or re.fullmatch(r'<[^<>\s@]+@[^<>\s@]+>', message_id) is None):
        raise MailDeliveryError()
    try:
        message = EmailMessage(policy=SMTP)
        message['From'] = Address(display_name=settings.get('from_name', 'StudiQ'), addr_spec=sender)
        message['To'] = Address(addr_spec=recipient)
        message['Subject'] = receipt.subject
        message['Message-ID'] = message_id
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        message.set_content(receipt.text_body)
        message.add_alternative(receipt.html_body, subtype='html')
        return message, sender, recipient
    except (TypeError, ValueError, UnicodeError):
        raise MailDeliveryError() from None


def save_preview(receipt, message):
    temporary = None
    try:
        receipt_id = str(UUID(str(receipt.id)))
        directory = INSTANCE / 'receipts'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / f'{receipt_id}.eml'
        if directory.is_symlink() or destination.is_symlink():
            raise MailDeliveryError()
        directory.chmod(0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f'.{receipt_id}-', suffix='.tmp', dir=directory)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(message.as_bytes())
        # Publish the complete preview so a download cannot read a partial email.
        os.replace(temporary, destination)
        temporary = None
        return 'preview'
    except (OSError, TypeError, ValueError):
        raise MailDeliveryError() from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def deliver_receipt(receipt, settings=None):
    settings = get_mail_settings() if settings is None else settings
    if not settings.get('ready'):
        raise MailDeliveryError('Receipt delivery is not configured. Please contact the administrator.')
    message, sender, recipient = build_message(receipt, settings)
    if settings.get('mode') == 'file':
        return save_preview(receipt, message)
    if settings.get('mode') != 'smtp' or settings.get('smtp_security') not in {'starttls', 'ssl'}:
        raise MailDeliveryError('Encrypted SMTP delivery is required.')
    sending = False
    try:
        context = ssl.create_default_context()
        if settings['smtp_security'] == 'ssl':
            connection = smtplib.SMTP_SSL(settings['smtp_host'], settings['smtp_port'], timeout=20, context=context)
        else:
            connection = smtplib.SMTP(settings['smtp_host'], settings['smtp_port'], timeout=20)
        with connection as server:
            server.ehlo()
            if settings['smtp_security'] == 'starttls':
                server.starttls(context=context)
                server.ehlo()
            if settings.get('smtp_username'):
                server.login(settings['smtp_username'], settings['smtp_password'])
            sending = True
            refused = server.send_message(message, from_addr=sender, to_addrs=[recipient])
            if refused:
                raise MailDeliveryError()
        return 'sent'
    except MailDeliveryError:
        raise
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPNotSupportedError):
        raise MailDeliveryError() from None
    except (smtplib.SMTPException, OSError, ValueError, UnicodeError):
        raise MailDeliveryError(uncertain=sending) from None


def render_receipt(details):
    try:
        amount = details['amount_minor']
        if type(amount) is not int or amount < 0 or details['currency'] != 'PHP' or details['mode'] not in {'test', 'live'}:
            raise MailDeliveryError('The receipt details are invalid.')
        paid_at = details['paid_at']
        if isinstance(paid_at, datetime):
            if paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            paid_at = paid_at.astimezone(ZoneInfo('Asia/Manila')).strftime('%B %d, %Y, %I:%M %p PHT')
        total = f'PHP {Decimal(amount) / 100:,.2f}'
        rows = [
            ('Customer', str(details['customer_name'])), ('Package', str(details['package_name'])),
            ('Workspace', str(details['workspace_code'])), ('Booking date', str(details['booking_date'])),
            ('Time slots', str(details['slots'])), ('Payment method', 'GCash via PayMongo'),
            ('Paid at (Manila time)', str(paid_at) if paid_at is not None else 'Not provided'),
            ('Amount paid', total), ('Receipt reference', str(details['receipt_id'])),
            ('Booking reference', str(details['booking_id'])), ('Payment reference', str(details['payment_id'])),
        ]
    except (KeyError, TypeError, ValueError):
        raise MailDeliveryError('The receipt details are invalid.') from None
    test = details['mode'] == 'test'
    subject = 'TEST — StudiQ payment receipt' if test else 'StudiQ payment receipt'
    banner = 'TEST RECEIPT — No real payment was charged.' if test else 'Payment received. Thank you for choosing StudiQ.'
    text_body = '\n'.join([subject, '', banner, '', *[f'{label}: {value}' for label, value in rows],
                           '', 'This is a payment receipt, not a tax invoice.'])
    table_rows = ''.join(
        '<tr><th style="padding:10px 12px;text-align:left;color:#6d677a;font-weight:normal;border-bottom:1px solid #eee">'
        + html.escape(label) + '</th><td style="padding:10px 12px;border-bottom:1px solid #eee;overflow-wrap:anywhere">'
        + html.escape(value) + '</td></tr>' for label, value in rows)
    html_body = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>' + html.escape(subject)
                 + '</title></head><body style="font-family:Arial,sans-serif;color:#29243b;background:#fcfbf8;padding:24px">'
                 '<main style="max-width:640px;margin:auto;background:white;border:1px solid #e9e5ee;border-radius:12px;padding:24px">'
                 '<p style="font-size:24px;font-weight:bold;color:#6950b9">StudiQ</p><h1 style="font-size:22px">'
                 + html.escape(subject) + '</h1><p style="padding:12px;background:#f1edf9">' + html.escape(banner)
                 + '</p><table style="width:100%;border-collapse:collapse">' + table_rows
                 + '</table><p style="color:#6d677a;font-size:12px">This is a payment receipt, not a tax invoice.</p></main></body></html>')
    return {'subject': subject, 'text_body': text_body, 'html_body': html_body}
