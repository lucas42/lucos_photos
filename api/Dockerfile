FROM python:3.14-slim

WORKDIR /app

COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/app/ ./app/

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
