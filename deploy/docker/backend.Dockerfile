# TestPilot AI backend image — used by the api, worker and migrate services.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

RUN groupadd --system --gid 10001 testpilot \
 && useradd --system --uid 10001 --gid testpilot --home /app --shell /usr/sbin/nologin testpilot \
 && mkdir -p /data/evidence && chown -R testpilot:testpilot /data

COPY backend/ /app/
RUN python -m compileall -q /app/testpilot

USER 10001:10001
EXPOSE 8000
# Default: API. The worker and migrate services override the command.
CMD ["uvicorn", "testpilot.api.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "2", "--timeout-keep-alive", "15"]
