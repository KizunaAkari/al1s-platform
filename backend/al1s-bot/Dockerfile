ARG NODE_IMAGE=m.daocloud.io/docker.io/library/node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
ARG PYTHON_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG NODE_CACHED_RUNTIME_IMAGE=m.daocloud.io/docker.io/library/node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
ARG NODE_BUILD_STAGE=build
FROM ${PYTHON_IMAGE} AS python-runtime
COPY al1s-backend/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements.lock supervisor==4.3.0

FROM ${NODE_IMAGE} AS dependencies

WORKDIR /app
COPY al1s-bot/package.json al1s-bot/package-lock.json ./
RUN npm ci

FROM dependencies AS build

COPY al1s-bot/tsconfig.json ./
COPY al1s-bot/src ./src
RUN npm run build && npm prune --omit=dev

# Optional offline packaging of a locally built, checked dist with identical runtime pins.
FROM ${NODE_CACHED_RUNTIME_IMAGE} AS node-cached
WORKDIR /app
COPY al1s-bot/package.json al1s-bot/package-lock.json ./
COPY al1s-bot/dist ./dist
RUN node -e "const fs=require('fs'),p=require('./package.json');for(const [n,v] of Object.entries(p.dependencies)){const m=JSON.parse(fs.readFileSync('./node_modules/'+n+'/package.json','utf8'));if(m.version!==v)throw new Error('runtime dependency mismatch: '+n)}"

FROM ${NODE_BUILD_STAGE} AS selected-node

FROM ${NODE_IMAGE} AS runtime

ENV NODE_ENV=production PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
WORKDIR /app

COPY --from=selected-node --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=selected-node --chown=node:node /app/node_modules ./node_modules
COPY --from=selected-node --chown=node:node /app/dist ./dist
COPY --from=python-runtime /usr/local /usr/local
COPY --from=python-runtime /usr/lib/x86_64-linux-gnu/ /usr/lib/x86_64-linux-gnu/
COPY --from=python-runtime /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --chown=node:node al1s-backend/al1s /opt/al1s/al1s
COPY --chown=node:node al1s-bot/supervisord.conf /etc/al1s-bot-supervisord.conf

RUN node --version && python -c "import ssl, sqlite3, PIL, fastapi, psycopg; assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0"

RUN mkdir -p /var/lib/al1s-plachta && chown node:node /var/lib/al1s-plachta

USER node
EXPOSE 3100 3101

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.HEALTH_PORT||3100)+'/health').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]

CMD ["supervisord", "-c", "/etc/al1s-bot-supervisord.conf"]
