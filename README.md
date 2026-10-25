# Common Ground — localhost baseline

A local foundation for the co-working booking system described in the design document.
Python 3.12, FastAPI, SQLAlchemy, SQLite, server-rendered HTML, and plain JavaScript.
No external accounts or credentials are required.

## Run locally

From the repository root on macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sh start.sh
```

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000` in your local browser. Interactive API documentation
is at `/docs`; database health is at `/health`. Stop with Ctrl+C.
The launcher binds to localhost and reloads on source changes. On macOS/Linux,
`PORT=8001 sh start.sh` selects another port. Set `HOST=0.0.0.0` only when you
intend to expose the development server to your network.

Startup creates `instance/coworking.db` and seeds eight shared desks, two private
rooms, Morning (09:00–12:00) and Afternoon (13:00–16:00) slots, and two sample
passes: ₱150 / 3 hours and ₱250 / day. These prices are illustrative, not business
pricing. Seeds are repeatable and preserve existing catalog prices and settings.
Dates use Asia/Manila. Both slots are required on the **same workspace** for a Day Pass.
An availability check does not hold inventory or create a booking.

## What works now

- Responsive homepage with database-backed pass and workspace selectors.
- Read-only availability, excluding inactive and allocated workspaces.
- Input validation, loading/error feedback, and accessible form labels.
- Allocation uniqueness, exclusive booking/maintenance ownership, and SQLite foreign-key checks.
- Tests covering availability, database constraints, and repeatable initialization.

Read-only APIs: `/api/packages`, `/api/workspace-types`, `/api/time-slots`, and
`/api/workspaces/availability?booking_date=YYYY-MM-DD&workspace_type_id=1&package_id=1&time_slot_id=1`.
For Day Pass (`package_id=2` in a fresh database), omit `time_slot_id`.
Availability returns counts only, never customer identities.

## Project structure

- `app/main.py`: application lifecycle, HTML route, health check.
- `app/api/catalog.py`: catalog and availability endpoints.
- `app/models/`: initial SQLAlchemy inventory and allocation schema.
- `app/services/availability.py`: workspace availability query.
- `app/database.py`, `app/config.py`, `app/seed.py`: local persistence and seed data.
- `templates/`, `static/`: presentation.
- `tests/`: isolated temporary SQLite database tests.

## Validate

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

There is no frontend build step. Data stays in the ignored `instance/` directory
across restarts. To reset sample data, stop the server, back up and remove
`instance/coworking.db`, then restart. Do not reset databases containing useful data.
`python -m app.seed` initializes the catalog without starting the server.

## Next stages

1. Add migrations (Alembic), profiles and booking/maintenance tables. Replace the
   provisional allocation owner strings with foreign keys. `create_all` initializes
   new tables only; it does not migrate existing schemas.
2. Add Supabase Auth and server-verified roles before any private customer or admin routes.
3. Implement transactional temporary holds and expiry/cancellation, using the database
   allocation constraint to reject concurrent reservations.
4. Add hosted checkout, signed idempotent payment webhooks and receipt delivery.
5. Add admin allocation grid, check-in roster, membership totals and maintenance controls.
6. Add the informational menu and move to PostgreSQL with migrations and access policies.

Login, reservation creation, payment, email, menu, and admin operations are intentionally
not implemented in this baseline. No Supabase project or other external database is modified.
`DATABASE_URL` can override the default SQLAlchemy URL, but PostgreSQL drivers, migrations,
and deployment configuration must be added and tested before connecting a live database.
The optional Render blueprint has no persistent storage configured; it is not a production deployment.
