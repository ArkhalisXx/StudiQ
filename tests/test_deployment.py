import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from textwrap import dedent

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import auth
from app.database import get_session, make_engine
from app.main import app
from app.models import User
from app.seed import initialize

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = {'full_name': 'Deployment Member', 'email': 'deployment@example.com',
           'password': 'a persistent account 42', 'password_confirmation': 'a persistent account 42'}


def isolated_python(source, instance):
    # A developer's .env or DATABASE_URL must never redirect these processes to real data.
    env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1'}
    env.pop('DATABASE_URL', None)
    if instance is None:
        env.pop('STUDIQ_INSTANCE_DIR', None)
    else:
        env['STUDIQ_INSTANCE_DIR'] = str(instance)
    return subprocess.run([sys.executable, '-c', dedent(source)], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=True, timeout=30)


@pytest.mark.parametrize('instance', [None, ''], ids=['unset', 'blank'])
def test_local_instance_default(instance):
    result = isolated_python('''
        import json
        from app.config import DATABASE_URL, INSTANCE
        print(json.dumps([str(INSTANCE), DATABASE_URL]))
    ''', instance)
    assert json.loads(result.stdout) == [str(ROOT / 'instance'),
                                       f'sqlite:///{ROOT / "instance" / "coworking.db"}']


def test_nested_volume_preserves_database_and_receipt_preview_across_processes(tmp_path):
    instance = tmp_path / 'volume' / 'nested'
    receipt_id = '317516c1-3abc-40a7-8cd3-a9e15bd51757'
    result = isolated_python(f'''
        import json
        from email.message import EmailMessage
        from types import SimpleNamespace
        from sqlalchemy.orm import Session
        from app.config import INSTANCE
        from app.database import engine
        from app.integrations.mail import save_preview
        from app.models import MenuItem
        from app.seed import initialize
        from app.services.receipts import receipt_path

        initialize()
        with Session(engine) as session, session.begin():
            session.add(MenuItem(name='Volume persistence check', description='Saved before restart',
                                 category='drinks', price_minor=12345))
        message = EmailMessage()
        message.set_content('Receipt saved before restart')
        assert save_preview(SimpleNamespace(id={receipt_id!r}), message) == 'preview'
        print(json.dumps([str(INSTANCE), engine.url.database, str(receipt_path({receipt_id!r}))]))
        engine.dispose()
    ''', instance)
    assert json.loads(result.stdout) == [str(instance), str(instance / 'coworking.db'),
                                       str(instance / 'receipts' / f'{receipt_id}.eml')]
    assert (instance / 'coworking.db').is_file()
    isolated_python(f'''
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from app.database import engine
        from app.models import MenuItem
        from app.seed import initialize
        from app.services.receipts import receipt_path

        initialize()
        with Session(engine) as session:
            item = session.scalar(select(MenuItem).where(MenuItem.name == 'Volume persistence check'))
            assert item is not None and item.price_minor == 12345
            assert item.description == 'Saved before restart'
        assert 'Receipt saved before restart' in receipt_path({receipt_id!r}).read_text()
        engine.dispose()
    ''', instance)


@pytest.mark.skipif(shutil.which('sh') is None, reason='The production launcher runs in a Linux container.')
@pytest.mark.parametrize('mount_path', ['', '/wrong-volume'], ids=['missing', 'mismatched'])
def test_railway_start_requires_matching_persistent_volume(tmp_path, mount_path):
    env = {**os.environ, 'RAILWAY_ENVIRONMENT_ID': 'deployment-test',
           'STUDIQ_INSTANCE_DIR': str(tmp_path / 'data'), 'RAILWAY_VOLUME_MOUNT_PATH': mount_path}
    result = subprocess.run(['sh', str(ROOT / 'start-production.sh')], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert 'Attach a Railway volume at the STUDIQ_INSTANCE_DIR path' in result.stderr


@pytest.fixture
def proxy_database(tmp_path, monkeypatch):
    engine = make_engine(f'sqlite:///{tmp_path / "proxy.db"}')
    initialize(engine)
    monkeypatch.setattr(auth, 'limiter', auth.AuthRateLimiter())

    def test_session():
        with Session(engine) as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, test_session)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize('trusted_hosts,origin,status,scheme', [
    ('*', 'https://testserver', 201, 'https'),
    ('127.0.0.1', 'https://testserver', 403, 'http'),
    ('*', 'https://other.example', 403, 'https'),
], ids=['trusted-https', 'untrusted-forwarding', 'cross-origin'])
def test_proxy_headers_preserve_https_and_origin_protection(proxy_database, trusted_hosts, origin, status, scheme):
    # Avoid the global app lifespan: requests and startup data both use the isolated database above.
    client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts),
                        headers={'X-Forwarded-Proto': 'https', 'Origin': origin})
    try:
        home = client.get('/')
        assert home.status_code == 200
        assert f'{scheme}://testserver/static/style.css' in home.text
        assert f'{scheme}://testserver/static/app.js' in home.text
        other_scheme = 'http' if scheme == 'https' else 'https'
        assert f'{other_scheme}://testserver/static/' not in home.text
        response = client.post('/api/auth/register', json=ACCOUNT)
        assert response.status_code == status, response.text
        if status == 201:
            assert 'Secure' in response.headers['set-cookie']
        else:
            assert 'set-cookie' not in response.headers
        with Session(proxy_database) as session:
            assert session.scalar(select(func.count()).select_from(User)) == (1 if status == 201 else 0)
    finally:
        client.close()
