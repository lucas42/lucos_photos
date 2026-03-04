#!/bin/sh
set -e
until pg_isready -h postgres -U "$POSTGRES_USER"; do
  echo "Waiting for postgres..."
  sleep 1
done
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
