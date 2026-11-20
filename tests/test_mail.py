from datetime import datetime
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from uuid import uuid4
import smtplib
import ssl

import pytest

from app.integrations import mail


@pytest.fixture(autouse=True)
def mail_environment(monkeypatch):
    for name in ['RECEIPT_DELIVERY', 'RECEIPT_FROM_EMAIL', 'RECEIPT_FROM_NAME', 'SMTP_HOST',
                 'SMTP_PORT', 'SMTP_SECURITY', 'SMTP_USERNAME', 'SMTP_PASSWORD']:
        monkeypatch.delenv(name, raising=False)


def sample_receipt():
    return SimpleNamespace(id=str(uuid4()), recipient='member@example.com',
                           subject='TEST — StudiQ payment receipt', text_body='TEST RECEIPT: PHP 150.00',
                           html_body='<h1>TEST RECEIPT</h1><p>PHP 150.00</p>',
                           message_id='<receipt.stable-id@studiq.local>')


def test_file_preview_works_with_blank_sender_from_example_env(monkeypatch, tmp_path):
    monkeypatch.setenv('RECEIPT_FROM_EMAIL', '')
    monkeypatch.setattr(mail, 'INSTANCE', tmp_path)
    receipt = sample_receipt()
    assert mail.get_mail_settings()['ready'] is True
    assert mail.deliver_receipt(receipt) == 'preview'
    path = tmp_path / 'receipts' / f'{receipt.id}.eml'
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    assert str(message['To']) == receipt.recipient
    assert str(message['Subject']) == receipt.subject
    assert message['Message-ID'] == receipt.message_id
    assert message.get_body(preferencelist=('plain',)).get_content().strip() == receipt.text_body
    assert receipt.html_body in message.get_body(preferencelist=('html',)).get_content()
    assert path.stat().st_mode & 0o777 == 0o600
    assert mail.deliver_receipt(receipt) == 'preview'
    assert len(list((tmp_path / 'receipts').glob('*.eml'))) == 1
    assert not list((tmp_path / 'receipts').glob('*.tmp'))


@pytest.mark.parametrize('field,value', [('recipient', 'member@example.com\nBcc: other@example.com'),
                                        ('subject', 'Receipt\r\nBcc: other@example.com'),
                                        ('message_id', '<broken id>')])
def test_email_headers_reject_injection(field, value):
    receipt = sample_receipt()
    setattr(receipt, field, value)
    with pytest.raises(mail.MailDeliveryError):
        mail.build_message(receipt, mail.get_mail_settings())


@pytest.mark.parametrize('value', ['../../outside', 'not-a-uuid'])
def test_preview_cannot_write_outside_receipts_directory(value, monkeypatch, tmp_path):
    monkeypatch.setattr(mail, 'INSTANCE', tmp_path)
    receipt = sample_receipt()
    receipt.id = value
    with pytest.raises(mail.MailDeliveryError):
        mail.deliver_receipt(receipt)
    assert not list(tmp_path.glob('*.eml'))


def test_preview_rejects_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(mail, 'INSTANCE', tmp_path)
    receipt = sample_receipt()
    (tmp_path / 'receipts').mkdir()
    outside = tmp_path / 'private.txt'
    outside.write_text('preserve this')
    (tmp_path / 'receipts' / f'{receipt.id}.eml').symlink_to(outside)
    with pytest.raises(mail.MailDeliveryError):
        mail.deliver_receipt(receipt)
    assert outside.read_text() == 'preserve this'


def smtp_environment(monkeypatch, security='starttls'):
    for name, value in {'RECEIPT_DELIVERY': 'smtp', 'SMTP_HOST': 'smtp.example.com',
                        'SMTP_SECURITY': security, 'RECEIPT_FROM_EMAIL': 'receipts@example.com',
                        'SMTP_USERNAME': 'synthetic-user', 'SMTP_PASSWORD': 'synthetic-password'}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize('name,value', [('SMTP_SECURITY', 'none'), ('SMTP_PORT', 'abc'),
                                      ('SMTP_PORT', '0'), ('SMTP_PORT', '65536'),
                                      ('SMTP_HOST', ''), ('SMTP_HOST', 'bad host'),
                                      ('SMTP_PASSWORD', ''), ('RECEIPT_FROM_EMAIL', ''),
                                      ('RECEIPT_FROM_NAME', 'Bad\nSender'), ('RECEIPT_DELIVERY', 'unknown')])
def test_invalid_smtp_configuration_does_not_attempt_delivery(name, value, monkeypatch):
    smtp_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(mail.smtplib, 'SMTP', lambda *args, **kwargs: pytest.fail('Unexpected SMTP connection'))
    assert mail.get_mail_settings()['ready'] is False
    with pytest.raises(mail.MailDeliveryError):
        mail.deliver_receipt(sample_receipt())


class FakeSMTP:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append('quit')

    def ehlo(self):
        self.calls.append('ehlo')

    def starttls(self, *, context):
        assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED
        self.calls.append('starttls')

    def login(self, username, password):
        assert username == 'synthetic-user' and password == 'synthetic-password'
        if self.failure == 'login':
            raise smtplib.SMTPAuthenticationError(535, b'synthetic provider error')
        self.calls.append('login')

    def send_message(self, message, *, from_addr, to_addrs):
        self.calls.append('send')
        self.messages.append((message, from_addr, to_addrs))
        if self.failure == 'send':
            raise smtplib.SMTPServerDisconnected('synthetic provider error')
        return {}


@pytest.mark.parametrize('security', ['starttls', 'ssl'])
def test_smtp_uses_encrypted_connection_and_stable_message_id(security, monkeypatch):
    smtp_environment(monkeypatch, security)
    server = FakeSMTP()

    def connect(host, port, *, timeout, **kwargs):
        assert host == 'smtp.example.com' and port == (465 if security == 'ssl' else 587)
        assert timeout == 20
        if security == 'ssl':
            assert kwargs['context'].verify_mode == ssl.CERT_REQUIRED
        return server

    monkeypatch.setattr(mail.smtplib, 'SMTP_SSL' if security == 'ssl' else 'SMTP', connect)
    receipt = sample_receipt()
    assert mail.deliver_receipt(receipt) == 'sent'
    assert server.calls == (['ehlo', 'starttls', 'ehlo', 'login', 'send', 'quit']
                            if security == 'starttls' else ['ehlo', 'login', 'send', 'quit'])
    message, sender, recipients = server.messages[0]
    assert message['Message-ID'] == receipt.message_id
    assert sender == 'receipts@example.com' and recipients == [receipt.recipient]


@pytest.mark.parametrize('failure,uncertain', [('login', False), ('send', True)])
def test_smtp_failures_are_sanitized_and_distinguish_unknown_delivery(failure, uncertain, monkeypatch):
    smtp_environment(monkeypatch)
    monkeypatch.setattr(mail.smtplib, 'SMTP', lambda *args, **kwargs: FakeSMTP(failure))
    with pytest.raises(mail.MailDeliveryError) as error:
        mail.deliver_receipt(sample_receipt())
    assert error.value.uncertain is uncertain
    assert 'synthetic' not in str(error.value)


@pytest.mark.parametrize('mode', ['test', 'live'])
def test_receipt_content_has_payment_details_manila_time_and_escaped_html(mode):
    details = {'receipt_id': 'receipt-id', 'booking_id': 'booking-id', 'payment_id': 'pay-id',
               'customer_name': '<script>alert(1)</script>', 'package_name': '3-Hour Pass',
               'workspace_code': 'DESK-01', 'booking_date': '2026-11-25', 'slots': 'Morning 09:00–12:00',
               'amount_minor': 15000, 'currency': 'PHP', 'mode': mode,
               'paid_at': datetime(2026, 11, 20, 3, 30)}
    content = mail.render_receipt(details)
    assert 'PHP 150.00' in content['text_body'] and '11:30 AM PHT' in content['text_body']
    assert all(value in content['text_body'] for value in ['booking-id', 'pay-id', 'GCash via PayMongo', 'DESK-01'])
    assert '<script>' not in content['html_body'] and '&lt;script&gt;' in content['html_body']
    assert ('No real payment was charged.' in content['text_body']) is (mode == 'test')
    assert content['subject'].startswith('TEST') is (mode == 'test')
