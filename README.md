# Hotel Booking API

A hotel booking backend focused on preventing double booking under concurrency.
FastAPI, SQLAlchemy 2.0, PostgreSQL 16.

> Work in progress. Architecture, API summary and experiment results will be added in
> Milestone 6.

## Local setup (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

Copy-Item .env.example .env        # then edit the dummy values

docker compose up -d db
python -m alembic upgrade head
python -m uvicorn app.main:app --reload
```

Health check: <http://localhost:8000/api/v1/health>. Interactive API docs: <http://localhost:8000/docs>

## Data and admin account

```powershell
python -m scripts.seed                    # 200 hotels, 10,000 rooms, 200,000 bookings
python -m scripts.seed --reset            # wipe everything (including admins) and reseed
python -m scripts.seed --hotels 20 --rooms 1000 --bookings 10000 --users 500   # smaller

python -m scripts.create_admin --email admin@example.com   # prompts for a password
```

Seeded users are `user1@example.com` … `userN@example.com` with the development password
`password123`.

## Tests and lint

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

Tests run against the `hotel_booking_test` database, which the db container creates on first
start. They rebuild the schema from the migrations, so they never touch development data.

Render the migration SQL without a database:

```powershell
python -m alembic upgrade head --sql
```
