"""Password hashing and opaque, database-backed browser sessions."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import AuthSession, User

COOKIE_NAME = 'common_ground_session'
SESSION_SECONDS = 7 * 24 * 60 * 60


def utc_now():
    # SQLite returns naive datetimes; consistently store UTC without an offset.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=3,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return f'scrypt${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    try:
        algorithm, salt, expected = encoded.split('$')
        if algorithm != 'scrypt':
            return False
        digest = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=32768, r=8, p=3,
                                maxmem=64 * 1024 * 1024, dklen=32)
        return secrets.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


# Unknown accounts take the same expensive verification path as known accounts.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def current_user(request: Request, session: Session = Depends(get_session)):
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 128:
        return None
    login = session.get(AuthSession, token_hash(token))
    if login is None or login.expires_at <= utc_now():
        return None
    return session.get(User, login.user_id)


def require_user(user: User | None = Depends(current_user)):
    if user is None:
        raise HTTPException(401, 'Please log in to continue.')
    return user


def create_session(request: Request, response: Response, session: Session, user: User):
    # Rotate an existing browser session and periodically discard expired records.
    previous = request.cookies.get(COOKIE_NAME)
    if previous and len(previous) <= 128:
        session.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(previous)))
    session.execute(delete(AuthSession).where(AuthSession.expires_at <= utc_now()))
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(token_hash=token_hash(token), user_id=user.id,
                            expires_at=utc_now() + timedelta(seconds=SESSION_SECONDS)))
    session.commit()
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == 'https', samesite='lax', path='/')


def public_user(user):
    return {'id': user.id, 'full_name': user.full_name, 'email': user.email}
