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
USER root

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
COPY --chown=node:node al1s-backend/requirements.lock /opt/al1s/requirements.lock
ARG RUNTIME_VERSION=1.0.0
RUN python -c "import json,hashlib,pathlib,os; p=pathlib.Path('/opt/al1s/al1s'); source=hashlib.sha256(b''.join(f.read_bytes() for f in sorted(p.rglob('*.py')))).hexdigest(); pathlib.Path('/opt/al1s/runtime-manifest.json').write_text(json.dumps(dict(role='discord',runtime_version=os.environ['RUNTIME_VERSION'],native_version=json.loads(pathlib.Path('/app/package.json').read_text())['version'],source_sha256=source,requirements_sha256=hashlib.sha256(pathlib.Path('/opt/al1s/requirements.lock').read_bytes()).hexdigest())))"
ENV PYTHONPATH=/opt/al1s AL1S_MEME_ROLE=discord
LABEL org.al1s.bot-role=discord org.al1s.runtime-version=${RUNTIME_VERSION} org.al1s.native-version=0.1.0
RUN python -m al1s.meme_workshop.runtime_probe

RUN node --version && python -c "import ssl, sqlite3, PIL, fastapi, psycopg; assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0"

RUN mkdir -p /var/lib/al1s-plachta && chown node:node /var/lib/al1s-plachta

USER node
EXPOSE 3100 3101

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-m", "al1s.meme_workshop.healthcheck"]

CMD ["supervisord", "-c", "/etc/al1s-bot-supervisord.conf"]
