from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.auth import same_origin
from app.database import get_session
from app.integrations.paymongo import WebhookSignatureError, verify_webhook
from app.models import User
from app.services.admin import require_admin
from app.services.auth import require_user
from app.services.payments import (get_settings, process_webhook, reconcile_checkout,
                                   require_settings, review_payments, start_checkout)
from app.services.reservations import serialize_booking

router = APIRouter(tags=['GCash payments'])
MAX_WEBHOOK_BYTES = 1024 * 1024


@router.get('/api/payments/config')
def payment_config():
    settings = get_settings()
    return {'enabled': settings['ready'], 'mode': settings['mode'] if settings['mode'] in {'test', 'live'} else 'test',
            'provider': 'PayMongo', 'method': 'GCash'}


@router.post('/api/checkout/{booking_id}', dependencies=[Depends(same_origin)])
def checkout(booking_id: UUID, user: User = Depends(require_user), session: Session = Depends(get_session)):
    return start_checkout(session, str(booking_id), user.id)


@router.post('/api/checkout/{booking_id}/reconcile', dependencies=[Depends(same_origin)])
def refresh_payment(booking_id: UUID, user: User = Depends(require_user), session: Session = Depends(get_session)):
    customer_id = user.id
    booking = reconcile_checkout(session, str(booking_id), customer_id)
    return serialize_booking(session, booking, customer_id)


@router.post('/api/payments/webhook')
async def payment_webhook(request: Request, session: Session = Depends(get_session)):
    settings = require_settings(allow_disabled=True)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, 'The payment event is too large.')
    try:
        event = verify_webhook(bytes(body), request.headers.get('paymongo-signature', ''),
                               settings['webhook_secret'], test_mode=settings['mode'] == 'test')
    except WebhookSignatureError:
        raise HTTPException(400, 'The payment event signature is invalid.') from None
    # Provider verification uses synchronous HTTP and SQLAlchemy, just like the other API services.
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(process_webhook, session, event)


@router.get('/api/admin/payments/review', dependencies=[Depends(require_admin)])
def payment_reviews(session: Session = Depends(get_session)):
    return review_payments(session)
