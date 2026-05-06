FROM lucas42/lucos_navbar:2.1.70 AS navbar

FROM node:25-alpine AS search-component
WORKDIR /build
RUN echo '{"dependencies":{"lucos_search_component":"^1.0.14"}}' > package.json && npm install

FROM python:3.14-slim
ARG VERSION
ENV VERSION=$VERSION

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=navbar lucos_navbar.js ./app/static/
COPY --from=search-component /build/node_modules/lucos_search_component/dist/index.js ./app/static/lucos_search_component.js

COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/alembic.ini .
COPY api/alembic/ ./alembic/
COPY api/entrypoint.sh .
COPY api/app/ ./app/

CMD ["sh", "entrypoint.sh"]
