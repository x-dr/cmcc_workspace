# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS node-deps
WORKDIR /build/recharge
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
COPY recharge/package.json recharge/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    LINGXI_DATA_DIR=/data \
    LINGXI_HOST=0.0.0.0 \
    LINGXI_PORT=5500
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata libstdc++6 libfontconfig1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lingxi \
    && useradd --uid 10001 --gid lingxi --no-create-home --home-dir /data lingxi \
    && install -d -m 0700 -o lingxi -g lingxi /data
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=node-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=node-deps /build/recharge/node_modules ./recharge/node_modules
COPY *.py app_login_public_key.json mobile_rights_gateway.json ./
COPY templates ./templates
COPY static ./static
COPY recharge ./recharge
COPY activities ./activities
USER 10001:10001
VOLUME ["/data"]
EXPOSE 5500
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import json, os, urllib.request; opener = urllib.request.build_opener(urllib.request.ProxyHandler({})); result = json.load(opener.open('http://127.0.0.1:' + os.environ['LINGXI_PORT'] + '/api/access/status', timeout=3)); assert result['local'] is True"]
CMD ["python", "-X", "utf8", "app.py"]
