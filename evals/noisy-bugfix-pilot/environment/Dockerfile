FROM python:3.12-slim

RUN pip install pytest

WORKDIR /app
COPY data/ /app/

RUN chmod +x /app/run_tests.sh
