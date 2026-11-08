FROM lucas42/lucos_navbar:2.3.2 AS navbar

FROM node:26-alpine AS search-component
WORKDIR /build
COPY api/search-component/package.json api/search-component/package-lock.json ./
RUN npm ci

FROM python:3.15-slim

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=navbar lucos_navbar.js ./app/static/
COPY --from=search-component /build/node_modules/lucos_search_component/dist/index.js ./app/static/lucos_search_component.js

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared package after the heavy install, so changes to it don't invalidate those layers.
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

# VERSION after the last RUN, so a version bump doesn't invalidate any layer above.
ARG VERSION
ENV VERSION=$VERSION

COPY api/alembic.ini .
COPY api/alembic/ ./alembic/
COPY api/entrypoint.sh .
COPY api/app/ ./app/

CMD ["sh", "entrypoint.sh"]
