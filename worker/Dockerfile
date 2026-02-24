FROM python:3.14-slim

WORKDIR /app

COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/app/ ./app/

CMD ["python", "-m", "app.main"]
