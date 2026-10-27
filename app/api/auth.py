from collections import defaultdict, deque
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import AuthSession, User
from app.services.auth import (COOKIE_NAME, DUMMY_PASSWORD_HASH, create_session, hash_password,
                               public_user, require_user, token_hash, utc_now, verify_password)


def origin_tuple(value):
    """Compare browser origins by scheme, hostname and effective port."""
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('Invalid origin')
    port = parsed.port if parsed.port is not None else {'http': 80, 'https': 443}[parsed.scheme]
    return parsed.scheme, parsed.hostname, port


def same_origin(request: Request):
    # Protect login, registration and logout from cross-site requests, including login CSRF.
    # Browser JSON POSTs supply Origin automatically; nonbrowser clients must send it explicitly.
    try:
        expected = f'{request.url.scheme}://{request.url.netloc}'
        matches = origin_tuple(request.headers.get('origin', '')) == origin_tuple(expected)
    except ValueError:
        matches = False
    if not matches:
        raise HTTPException(403, 'Please submit this request from this website.')


class AuthRateLimiter:
    """Small single-process limiter for the localhost development server."""
    def __init__(self):
        self.attempts = defaultdict(deque)
        self.lock = Lock()

    def check(self, address):
        now = monotonic()
        with self.lock:
            for key in list(self.attempts):
                queue = self.attempts[key]
                while queue and queue[0] <= now - 60:
                    queue.popleft()
                if not queue:
                    del self.attempts[key]
            queue = self.attempts[address]
            if len(queue) >= 10:
                raise HTTPException(429, 'Too many attempts. Please try again in a minute.',
                                    headers={'Retry-After': '60'})
            queue.append(now)


limiter = AuthRateLimiter()


def limit_attempts(request: Request):
    limiter.check(request.client.host if request.client else 'local')


router = APIRouter(prefix='/api/auth', tags=['Accounts'], dependencies=[Depends(same_origin)])


class LoginInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: EmailStr
    password: SecretStr

    @field_validator('email', mode='before')
    @classmethod
    def normalize_email(cls, value):
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator('email')
    @classmethod
    def bound_email(cls, value):
        if len(value) > 254:
            raise ValueError('Email must be at most 254 characters.')
        return value

    @field_validator('password')
    @classmethod
    def bound_password(cls, value):
        password = value.get_secret_value()
        if not 1 <= len(password) <= 128:
            raise ValueError('Password must be between 1 and 128 characters.')
        try:
            password.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError('Password contains invalid Unicode characters.') from None
        return value


class RegistrationInput(LoginInput):
    full_name: str = Field(min_length=1, max_length=80)
    password_confirmation: SecretStr

    @field_validator('full_name', mode='before')
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('password')
    @classmethod
    def strong_password(cls, value):
        if len(value.get_secret_value()) < 12:
            raise ValueError('Use at least 12 characters for your password.')
        return value

    @model_validator(mode='after')
    def matching_passwords(self):
        if self.password.get_secret_value() != self.password_confirmation.get_secret_value():
            raise ValueError('Passwords do not match.')
        return self


@router.post('/register', status_code=201, dependencies=[Depends(limit_attempts)])
def register(data: RegistrationInput, request: Request, response: Response,
             session: Session = Depends(get_session)):
    user = User(full_name=data.full_name, email=str(data.email),
                password_hash=hash_password(data.password.get_secret_value()), created_at=utc_now())
    session.add(user)
    try:
        session.flush()
        create_session(request, response, session, user)
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, 'An account with this email already exists. Please log in.') from None
    return public_user(user)


@router.post('/login', dependencies=[Depends(limit_attempts)])
def login(data: LoginInput, request: Request, response: Response, session: Session = Depends(get_session)):
    user = session.scalar(select(User).where(User.email == str(data.email)))
    encoded = user.password_hash if user else DUMMY_PASSWORD_HASH
    valid = verify_password(data.password.get_secret_value(), encoded)
    if user is None or not valid:
        raise HTTPException(401, 'Email or password is incorrect.')
    create_session(request, response, session, user)
    return public_user(user)


@router.post('/logout', status_code=204)
def logout(request: Request, session: Session = Depends(get_session)):
    token = request.cookies.get(COOKIE_NAME)
    if token and len(token) <= 128:
        session.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        session.commit()
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path='/', httponly=True,
                           secure=request.url.scheme == 'https', samesite='lax')
    return response


# Read-only profile has no CSRF requirement, so it is intentionally separate from the POST router.
profile_router = APIRouter(prefix='/api/auth', tags=['Accounts'])


@profile_router.get('/me')
def me(user: User = Depends(require_user)):
    return public_user(user)
