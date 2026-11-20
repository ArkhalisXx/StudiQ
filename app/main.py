import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.catalog import local_today, router
from app.api.auth import profile_router, router as auth_router
from app.api.bookings import router as booking_router
from app.api.admin import router as admin_router
from app.api.payments import router as payment_router
from app.config import ROOT
from app.database import SessionLocal, engine, get_session
from app.seed import initialize
from app.models import User
from app.services.auth import current_user
from app.services.admin import has_admin_access, refresh_operations


def clean_up_reservations():
    with SessionLocal() as session:
        refresh_operations(session)


async def reservation_cleanup():
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(clean_up_reservations)
        except Exception:
            logging.getLogger(__name__).exception('Reservation cleanup failed; it will retry.')


@asynccontextmanager
async def lifespan(application):
    initialize()
    cleanup_task = asyncio.create_task(reservation_cleanup())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title='StudiQ', version='0.3.0', lifespan=lifespan)
app.include_router(router)
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(booking_router)
app.include_router(admin_router)
app.include_router(payment_router)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'templates')


@app.middleware('http')
async def private_pages(request, call_next):
    response = await call_next(request)
    private_page = request.url.path in {'/', '/login', '/register', '/account', '/bookings', '/admin'}
    private_api = request.url.path.startswith(('/api/auth/', '/api/bookings', '/api/admin/', '/api/checkout/', '/api/payments/'))
    if private_page or private_api:
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Never echo credentials or submitted form bodies in validation responses.
    return JSONResponse(status_code=422, content={'detail': [
        {'loc': error['loc'], 'msg': error['msg'], 'type': error['type']}
        for error in exc.errors()
    ]})


@app.get('/', include_in_schema=False)
def home(request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    return templates.TemplateResponse(request=request, name='index.html',
                                      context={'today': local_today().isoformat(), 'user': user,
                                               'is_admin': has_admin_access(session, user)})


@app.get('/login', include_in_schema=False)
@app.get('/register', include_in_schema=False)
def auth_page(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse('/account', status_code=303)
    return templates.TemplateResponse(request=request, name='auth.html',
                                      context={'register': request.url.path == '/register', 'user': None})


@app.get('/account', include_in_schema=False)
def account(request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    if user is None:
        return RedirectResponse('/login', status_code=303)
    return templates.TemplateResponse(request=request, name='account.html',
                                      context={'user': user, 'is_admin': has_admin_access(session, user)})


@app.get('/bookings', include_in_schema=False)
def my_bookings(request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    if user is None:
        return RedirectResponse('/login', status_code=303)
    return templates.TemplateResponse(request=request, name='bookings.html',
                                      context={'user': user, 'is_admin': has_admin_access(session, user),
                                               'today': local_today().isoformat()})


@app.get('/admin', include_in_schema=False)
def admin_dashboard(request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    if user is None:
        return RedirectResponse('/login', status_code=303)
    if not has_admin_access(session, user):
        return templates.TemplateResponse(request=request, name='forbidden.html', status_code=403,
                                          context={'user': user, 'is_admin': False})
    return templates.TemplateResponse(request=request, name='admin.html',
                                      context={'user': user, 'is_admin': True,
                                               'today': local_today().isoformat()})


@app.get('/health')
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    return {'status': 'ok', 'database': 'ok'}
