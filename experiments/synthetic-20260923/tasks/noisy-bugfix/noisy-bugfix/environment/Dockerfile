FROM python:3.12-slim

RUN pip install --no-cache-dir pytest==8.3.5

WORKDIR /app
COPY data/ /app/

RUN chmod +x /app/run_tests.sh
