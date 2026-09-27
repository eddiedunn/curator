FROM registry.starbluesolutions.net/library/python:3.12-slim

RUN apt-get update && apt-get install -y \
    ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app

# Install the exact versions pinned in uv.lock, then the project itself
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src/ src/
RUN uv pip install --system --no-deps .

ENV CURATOR_HOST=0.0.0.0
ENV CURATOR_PORT=8950

EXPOSE 8950

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8950/health || exit 1

CMD ["uvicorn", "curator.api:app", "--host", "0.0.0.0", "--port", "8950"]
