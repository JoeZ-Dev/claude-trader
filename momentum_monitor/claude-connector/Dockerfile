# Build context is momentum_monitor/ (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV HOME=/root

COPY claude-connector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The `claude` CLI itself -- the native, non-npm installer (specs.md
# section 25's stage-0 investigation confirmed the host's own install is
# this same native binary, no Node.js runtime needed). Auth is
# deliberately NOT baked into the image: it comes from a read-only
# bind-mount of the host's ~/.claude/.credentials.json at container
# RUNTIME (docker-compose.yml) -- the minimal file the investigation
# proved sufficient; nothing else from the host's ~/.claude is mounted
# or needed. curl/ca-certificates are purged again after install so they
# don't linger in the final image.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.local/bin:${PATH}"

COPY claude-connector/ .

EXPOSE 7879
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7879"]
