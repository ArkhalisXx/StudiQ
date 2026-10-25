from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.api.catalog import local_today, router
from app.config import ROOT
from app.database import engine
from app.seed import initialize


@asynccontextmanager
async def lifespan(application):
    initialize()
    yield


app = FastAPI(title='Common Ground', version='0.1.0', lifespan=lifespan)
app.include_router(router)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'templates')


@app.get('/', include_in_schema=False)
def home(request: Request):
    return templates.TemplateResponse(request=request, name='index.html', context={'today': local_today().isoformat()})


@app.get('/health')
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    return {'status': 'ok', 'database': 'ok'}
