# Build context is momentum_monitor/ (see docker-compose.yml) so that the
# pure analysis core can be copied in alongside the app.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV CORE_PATH=/app/core

COPY monitor-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor-app/ .
COPY core/ ./core/

EXPOSE 8012
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8012"]
