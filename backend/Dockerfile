FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/cta-v6-entrypoint
RUN chmod 0755 /usr/local/bin/cta-v6-entrypoint \
    && chown -R app:app /app

USER app
EXPOSE 8060

ENTRYPOINT ["cta-v6-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8060", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
