FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN python -m pip install --no-cache-dir .

CMD ["python", "-m", "pyfuse", "worker", "--backend", "http://pyfuse-cloud-api.pyfuse-cloud.svc.cluster.local:8000/api/v1/broker"]
