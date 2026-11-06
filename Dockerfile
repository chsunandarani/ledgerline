FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
ENV PORT=8000
CMD python -m scripts.init_db && uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers
