from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import auth
from app.database import get_session, make_engine
from app.main import app
from app.models import AuthSession, User, Workspace
from app.seed import initialize
from app.services.auth import COOKIE_NAME, token_hash, utc_now, verify_password

PASSWORD = 'a few memorable words 42'
ACCOUNT = {'full_name': 'Test Member', 'email': 'member@example.com', 'password': PASSWORD,
           'password_confirmation': PASSWORD}


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    engine = make_engine(f'sqlite:///{tmp_path / "accounts.db"}')
    initialize(engine)
    monkeypatch.setattr(auth, 'limiter', auth.AuthRateLimiter())

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    try:
        with TestClient(app, headers={'Origin': 'http://testserver'}) as client:
            yield client, engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def register(client, **changes):
    return client.post('/api/auth/register', json={**ACCOUNT, **changes})


def test_registration_hashes_password_and_creates_session(accounts):
    client, engine = accounts
    response = register(client, full_name='  Test Member  ', email='  MEMBER@Example.COM ')
    assert response.status_code == 201
    assert response.json()['full_name'] == 'Test Member'
    assert response.json()['email'] == 'member@example.com'
    assert set(response.json()) == {'id', 'full_name', 'email'}
    assert response.headers['cache-control'] == 'no-store'
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Max-Age=604800' in cookie
    token = client.cookies[COOKIE_NAME]
    with Session(engine) as session:
        user = session.scalar(select(User))
        assert user.password_hash != PASSWORD
        assert verify_password(PASSWORD, user.password_hash)
        saved_session = session.scalar(select(AuthSession))
        assert saved_session.token_hash == token_hash(token)
        assert saved_session.token_hash != token
    assert client.get('/api/auth/me').json()['id'] == response.json()['id']
    page = client.get('/account')
    assert 'Test Member' in page.text and 'member@example.com' in page.text
    assert page.headers['cache-control'] == 'no-store'
    assert client.get('/login', follow_redirects=False).headers['location'] == '/account'


def test_duplicate_email_is_case_insensitive(accounts):
    client, engine = accounts
    assert register(client).status_code == 201
    assert register(client, email='MEMBER@EXAMPLE.COM').status_code == 409
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize('surrogate', ['\ud800', '\udfff'])
def test_invalid_unicode_password_is_rejected_without_a_server_error(accounts, surrogate):
    client, engine = accounts
    password = PASSWORD + surrogate
    payload = {**ACCOUNT, 'password': password, 'password_confirmation': password}
    # JSON escapes can carry a lone surrogate even though ordinary UTF-8 cannot.
    response = client.post('/api/auth/register', content=json.dumps(payload),
                           headers={'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert 'invalid Unicode characters' in response.text
    assert PASSWORD not in response.text
    assert COOKIE_NAME not in client.cookies
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.parametrize('surrogate', ['\ud800', '\udfff'])
def test_invalid_unicode_name_is_rejected_before_creating_an_account(accounts, surrogate):
    client, engine = accounts
    payload = {**ACCOUNT, 'full_name': 'Member' + surrogate}
    response = client.post('/api/auth/register', content=json.dumps(payload),
                           headers={'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['type'] == 'string_unicode'
    assert PASSWORD not in response.text
    assert COOKIE_NAME not in client.cookies
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.parametrize('scheme,host,origin', [
    ('http', 'testserver:80', 'http://testserver'),
    ('http', 'testserver', 'http://testserver:80'),
    ('http', 'TESTSERVER:80', 'http://testserver'),
    ('https', 'testserver:443', 'https://testserver'),
    ('https', 'testserver', 'https://testserver:443'),
])
def test_equivalent_origins_allow_authentication(accounts, scheme, host, origin):
    client, _ = accounts
    response = client.post(f'{scheme}://testserver/api/auth/register', json=ACCOUNT,
                           headers={'Host': host, 'Origin': origin})
    assert response.status_code == 201


@pytest.mark.parametrize('origin', [
    'https://testserver',
    'http://testserver:8001',
    'http://evil.example@testserver',
    'http://testserver/extra',
    'http://testserver?extra',
    'http://testserver#extra',
    'http://testserver:bad',
])
def test_origin_normalization_does_not_allow_different_or_malformed_origins(accounts, origin):
    client, _ = accounts
    assert client.post('/api/auth/register', json=ACCOUNT, headers={'Origin': origin}).status_code == 403


@pytest.mark.parametrize('changes', [
    {'email': 'invalid'},
    {'full_name': '   '},
    {'full_name': 'A' * 81},
    {'password': 'short', 'password_confirmation': 'short'},
    {'password': 'A' * 129, 'password_confirmation': 'A' * 129},
    {'password_confirmation': 'something different'},
    {'role': 'admin'},
])
def test_registration_validation_does_not_create_user_or_echo_credentials(accounts, changes):
    client, engine = accounts
    response = register(client, **changes)
    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert 'input' not in response.json()['detail'][0]
    assert COOKIE_NAME not in response.cookies
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


def test_login_logout_revocation_and_rotation(accounts):
    client, engine = accounts
    register(client)
    first_token = client.cookies[COOKIE_NAME]
    login = client.post('/api/auth/login', json={'email': 'MEMBER@example.com', 'password': PASSWORD})
    assert login.status_code == 200
    second_token = client.cookies[COOKIE_NAME]
    assert first_token != second_token
    with Session(engine) as session:
        assert session.get(AuthSession, token_hash(first_token)) is None
    # A fresh browser can reuse the persistent session without logging in again.
    with TestClient(app) as another_browser:
        another_browser.cookies.set(COOKIE_NAME, second_token)
        assert another_browser.get('/api/auth/me').status_code == 200
    response = client.post('/api/auth/logout')
    assert response.status_code == 204
    assert COOKIE_NAME not in client.cookies
    assert client.get('/api/auth/me').status_code == 401
    # A stolen/cached copy of the cookie must also be unusable after logout.
    client.cookies.set(COOKIE_NAME, second_token)
    assert client.get('/api/auth/me').status_code == 401
    client.cookies.clear()
    assert client.post('/api/auth/logout').status_code == 204
    assert client.post('/api/auth/login', json={'email': ACCOUNT['email'], 'password': PASSWORD}).status_code == 200


def test_wrong_and_unknown_passwords_have_same_error(accounts):
    client, _ = accounts
    register(client)
    client.post('/api/auth/logout')
    wrong = client.post('/api/auth/login', json={'email': ACCOUNT['email'], 'password': 'wrong password'})
    unknown = client.post('/api/auth/login', json={'email': 'unknown@example.com', 'password': PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert COOKIE_NAME not in client.cookies


def test_expired_and_forged_sessions_are_rejected(accounts):
    client, engine = accounts
    register(client)
    with Session(engine) as session, session.begin():
        session.scalar(select(AuthSession)).expires_at = utc_now() - timedelta(seconds=1)
    assert client.get('/api/auth/me').status_code == 401
    assert client.get('/account', follow_redirects=False).headers['location'] == '/login'
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, 'not-a-real-session')
    assert client.get('/api/auth/me').status_code == 401


@pytest.mark.parametrize('path', ['/api/auth/register', '/api/auth/login', '/api/auth/logout'])
@pytest.mark.parametrize('origin', ['https://other.example', 'null', ''])
def test_auth_mutations_require_same_origin(accounts, path, origin):
    response = accounts[0].post(path, json=ACCOUNT, headers={'Origin': origin})
    assert response.status_code == 403


def test_logout_csrf_does_not_destroy_session(accounts):
    client, _ = accounts
    register(client)
    assert client.post('/api/auth/logout', headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.get('/api/auth/me').status_code == 200


def test_auth_throttling(accounts):
    client, _ = accounts
    for _ in range(10):
        assert register(client, email='bad').status_code == 422
    blocked = register(client)
    assert blocked.status_code == 429
    assert blocked.headers['retry-after'] == '60'
    assert client.get('/api/packages').status_code == 200
    assert client.post('/api/auth/logout').status_code == 204


def test_https_cookie_is_secure(accounts):
    with TestClient(app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        response = register(client)
        assert response.status_code == 201
        assert 'Secure' in response.headers['set-cookie']
        assert client.get('/api/auth/me').status_code == 200


def test_seed_preserves_accounts_and_inventory(accounts):
    client, engine = accounts
    user_id = register(client).json()['id']
    initialize(engine)
    with Session(engine) as session:
        assert session.get(User, user_id).email == ACCOUNT['email']
        assert session.scalar(select(func.count()).select_from(Workspace)) == 10
    assert client.get('/api/auth/me').status_code == 200


def test_html_escapes_user_names(accounts):
    client, _ = accounts
    register(client, full_name='<script>alert(1)</script>')
    page = client.get('/account')
    assert '<script>alert(1)</script>' not in page.text
    assert '&lt;script&gt;' in page.text


def test_anonymous_pages_and_protected_account(accounts):
    client, _ = accounts
    assert client.get('/account', follow_redirects=False).headers['location'] == '/login'
    assert client.get('/api/auth/me').status_code == 401
    for path, text in [('/login', 'current-password'), ('/register', 'new-password')]:
        response = client.get(path)
        assert response.status_code == 200 and text in response.text
        assert response.headers['cache-control'] == 'no-store'
    assert client.get('/').status_code == 200
    assert client.get('/api/packages').status_code == 200
