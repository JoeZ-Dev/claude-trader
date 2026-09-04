# Build context is momentum_monitor/ (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY schwab-connector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schwab-connector/ .
COPY fixtures/ ./fixtures/

EXPOSE 7878
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7878"]
