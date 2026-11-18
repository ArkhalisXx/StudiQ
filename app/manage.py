"""Trusted local administration commands; no administrator is created by default."""
import argparse

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import AdminGrant, User
from app.seed import initialize
from app.services.auth import utc_now


def set_admin(email, enabled, target=engine):
    normalized = str(TypeAdapter(EmailStr).validate_python(email.strip().casefold()))
    initialize(target)
    with Session(target) as session, session.begin():
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            raise ValueError('No registered user has that email address. Register the account first.')
        grant = session.get(AdminGrant, user.id)
        if enabled and grant is None:
            session.add(AdminGrant(user_id=user.id, created_at=utc_now()))
        elif not enabled and grant is not None:
            session.delete(grant)


def main():
    parser = argparse.ArgumentParser(description='Manage StudiQ administrator access locally.')
    parser.add_argument('command', choices=['grant-admin', 'revoke-admin'])
    parser.add_argument('email', help='Email address of an existing registered user')
    arguments = parser.parse_args()
    try:
        set_admin(arguments.email, arguments.command == 'grant-admin')
    except ValueError as error:
        parser.exit(1, f'{error}\n')
    print('Administrator access granted.' if arguments.command == 'grant-admin' else 'Administrator access revoked.')


if __name__ == '__main__':
    main()
