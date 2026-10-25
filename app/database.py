from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL, INSTANCE


class Base(DeclarativeBase):
    pass


def make_engine(url):
    engine = create_engine(url, connect_args={'check_same_thread': False} if url.startswith('sqlite') else {})
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
    return engine


INSTANCE.mkdir(exist_ok=True)
engine = make_engine(DATABASE_URL)
SessionLocal = sessionmaker(engine)


def get_session():
    with SessionLocal() as session:
        yield session
