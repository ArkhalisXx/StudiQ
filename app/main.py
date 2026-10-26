from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.api.catalog import local_today, router
from app.api.auth import profile_router, router as auth_router
from app.config import ROOT
from app.database import engine
from app.seed import initialize
from app.models import User
from app.services.auth import current_user


@asynccontextmanager
async def lifespan(application):
    initialize()
    yield


app = FastAPI(title='StudiQ', version='0.1.0', lifespan=lifespan)
app.include_router(router)
app.include_router(auth_router)
app.include_router(profile_router)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'templates')


@app.middleware('http')
async def private_pages(request, call_next):
    response = await call_next(request)
    if request.url.path in {'/', '/login', '/register', '/account'} or request.url.path.startswith('/api/auth/'):
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
def home(request: Request, user: User | None = Depends(current_user)):
    return templates.TemplateResponse(request=request, name='index.html',
                                      context={'today': local_today().isoformat(), 'user': user})


@app.get('/login', include_in_schema=False)
@app.get('/register', include_in_schema=False)
def auth_page(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse('/account', status_code=303)
    return templates.TemplateResponse(request=request, name='auth.html',
                                      context={'register': request.url.path == '/register', 'user': None})


@app.get('/account', include_in_schema=False)
def account(request: Request, user: User | None = Depends(current_user)):
    if user is None:
        return RedirectResponse('/login', status_code=303)
    return templates.TemplateResponse(request=request, name='account.html', context={'user': user})


@app.get('/health')
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    return {'status': 'ok', 'database': 'ok'}
