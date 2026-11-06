FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Don't run as root inside the container.
RUN useradd --create-home --uid 10001 ledger
USER ledger

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# init_db is idempotent: creates schema + seed on first start, no-op afterwards.
CMD ["sh", "-c", "python -m scripts.init_db && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
