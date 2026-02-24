FROM python:3.14-slim

WORKDIR /app

COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/alembic.ini .
COPY api/alembic/ ./alembic/
COPY api/entrypoint.sh .
COPY api/app/ ./app/

CMD ["sh", "entrypoint.sh"]
