from fastapi.testclient import TestClient

from app.main import app
from test_admin import operations


def test_anyone_can_browse_menu_but_management_requires_an_admin(operations):
    staff, customer, _, _, _ = operations
    with TestClient(app) as anonymous:
        response = anonymous.get('/menu')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert 'href="/menu"' in response.text
        assert 'href="/admin/menu"' not in response.text
        protected = anonymous.get('/admin/menu', follow_redirects=False)
        assert protected.status_code == 303 and protected.headers['location'] == '/login'
    assert customer.get('/menu').status_code == 200
    assert customer.get('/admin/menu').status_code == 403
    admin_page = staff.get('/admin/menu')
    assert admin_page.status_code == 200
    assert admin_page.headers['cache-control'] == 'no-store'
    assert 'admin-menu.js' in admin_page.text
    assert 'href="/admin/menu"' in staff.get('/menu').text
    assert 'href="/admin/menu"' in staff.get('/admin').text
