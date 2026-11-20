"""Upgrade the local SQLite database without resetting existing records."""
import os
from pathlib import Path
import sqlite3
import tempfile

from sqlalchemy import inspect

from app.database import Base

SCHEMA_VERSION = 4
ALLOCATION_COLUMNS = {'id', 'booking_date', 'workspace_id', 'time_slot_id', 'booking_id', 'maintenance_block_id'}


class MigrationError(RuntimeError):
    pass


def _needs_allocation_upgrade(connection):
    inspector = inspect(connection)
    if 'slot_allocations' not in inspector.get_table_names():
        return False
    owners = {tuple(fk['constrained_columns']): fk['referred_table']
              for fk in inspector.get_foreign_keys('slot_allocations')}
    return owners.get(('booking_id',)) != 'bookings' or owners.get(('maintenance_block_id',)) != 'maintenance_blocks'


def _backup(connection, target):
    database = target.url.database
    if database and database != ':memory:' and not database.startswith('file:'):
        source = Path(database).resolve()
        folder = source.parent / 'backups'
        folder.mkdir(mode=0o700, exist_ok=True)
        prefix = f'{source.stem}-before-reservations-v1-'
    else:
        folder = Path(tempfile.gettempdir())
        prefix = 'studiq-before-reservations-v1-'
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix='.sqlite3', dir=folder)
    os.close(descriptor)
    path = Path(name)
    try:
        with sqlite3.connect(path) as destination:
            connection.connection.driver_connection.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise MigrationError('The database backup did not pass its integrity check.')
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _upgrade_allocations(connection):
    columns = {column['name'] for column in inspect(connection).get_columns('slot_allocations')}
    if columns != ALLOCATION_COLUMNS:
        raise MigrationError('The legacy allocation table has unexpected columns; no records were changed.')
    for owner_column, parent_table in [('booking_id', 'bookings'), ('maintenance_block_id', 'maintenance_blocks')]:
        orphan = connection.exec_driver_sql(
            f'SELECT 1 FROM slot_allocations AS a LEFT JOIN {parent_table} AS p '
            f'ON a.{owner_column} = p.id WHERE a.{owner_column} IS NOT NULL AND p.id IS NULL LIMIT 1'
        ).first()
        if orphan:
            raise MigrationError(
                f'Legacy allocations reference missing {parent_table}. '
                'Resolve the owner records before retrying; existing allocations were preserved.'
            )
    saved_objects = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE tbl_name = 'slot_allocations' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
    ).scalars().all()
    connection.exec_driver_sql('''CREATE TABLE slot_allocations_v1 (
        id INTEGER NOT NULL PRIMARY KEY,
        booking_date DATE NOT NULL,
        workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
        time_slot_id INTEGER NOT NULL REFERENCES time_slots(id),
        booking_id VARCHAR(36) REFERENCES bookings(id),
        maintenance_block_id VARCHAR(36) REFERENCES maintenance_blocks(id),
        UNIQUE (booking_date, workspace_id, time_slot_id),
        CHECK ((booking_id IS NOT NULL) <> (maintenance_block_id IS NOT NULL))
    )''')
    connection.exec_driver_sql('''INSERT INTO slot_allocations_v1
        (id, booking_date, workspace_id, time_slot_id, booking_id, maintenance_block_id)
        SELECT id, booking_date, workspace_id, time_slot_id, booking_id, maintenance_block_id
        FROM slot_allocations''')
    connection.exec_driver_sql('DROP TABLE slot_allocations')
    connection.exec_driver_sql('ALTER TABLE slot_allocations_v1 RENAME TO slot_allocations')
    for statement in saved_objects:
        connection.exec_driver_sql(statement)


def upgrade(target):
    """Create fresh tables, repair legacy allocation FKs, and add later feature tables."""
    # Import registers all models before creating or upgrading tables.
    import app.models  # noqa: F401

    if target.dialect.name != 'sqlite':
        raise MigrationError('Automatic schema upgrades currently support SQLite only.')
    backup_path = None
    with target.connect() as connection:
        tables = inspect(connection).get_table_names()
        if 'schema_migrations' in tables:
            version = connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar()
            if version is not None and version > SCHEMA_VERSION:
                raise MigrationError('This database requires a newer application version.')
        needs_upgrade = _needs_allocation_upgrade(connection)
        connection.rollback()
        if needs_upgrade:
            backup_path = _backup(connection, target)
        try:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            Base.metadata.create_all(connection)
            if _needs_allocation_upgrade(connection):
                _upgrade_allocations(connection)
            if connection.exec_driver_sql('PRAGMA foreign_key_check').first():
                raise MigrationError('Foreign-key validation failed; the database upgrade was rolled back.')
            connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER NOT NULL PRIMARY KEY,
                applied_at TEXT NOT NULL
            )''')
            for version in range(1, SCHEMA_VERSION + 1):
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (version,),
                )
            connection.commit()
        except Exception as error:
            connection.rollback()
            if backup_path and isinstance(error, MigrationError):
                raise MigrationError(f'{error} Backup: {backup_path}') from error
            raise
    return backup_path
