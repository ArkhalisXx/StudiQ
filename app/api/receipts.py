from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.auth import same_origin
from app.database import get_session
from app.models import User
from app.services.auth import require_user
from app.services.receipts import preview_receipt, request_receipt
from app.services.reservations import serialize_booking

router = APIRouter(tags=['Email receipts'])


@router.post('/api/checkout/{booking_id}/receipt', dependencies=[Depends(same_origin)])
def email_receipt(booking_id: UUID, user: User = Depends(require_user), session: Session = Depends(get_session)):
    customer_id = user.id
    booking = request_receipt(session, str(booking_id), customer_id)
    return serialize_booking(session, booking, customer_id)


@router.get('/api/receipts/{receipt_id}/preview')
def receipt_preview(receipt_id: UUID, user: User = Depends(require_user), session: Session = Depends(get_session)):
    path = preview_receipt(session, str(receipt_id), user.id)
    return FileResponse(path, media_type='message/rfc822', filename=f'StudiQ-receipt-{receipt_id}.eml',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
