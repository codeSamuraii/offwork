FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN python -m pip install --no-cache-dir -r /app/cloud_poc/backend/requirements.txt && \
    python -m pip install --no-cache-dir /app

CMD ["uvicorn", "cloud_poc.backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
