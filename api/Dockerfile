FROM lucas42/lucos_navbar:2.1.3 AS navbar
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=navbar lucos_navbar.js ./app/static/

COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/alembic.ini .
COPY api/alembic/ ./alembic/
COPY api/entrypoint.sh .
COPY api/app/ ./app/

CMD ["sh", "entrypoint.sh"]
