# syntax=docker/dockerfile:1

# Build stage: install dependencies into a virtualenv. pip, its cache and anything
# downloaded during the install stay in this stage.
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && pip uninstall --yes pip setuptools


# Runtime stage: the virtualenv and the application code, run as an unprivileged user.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts

USER app
EXPOSE 8000

# Only nginx can reach the API (it isn't published on the host), so trusting forwarded
# headers from any address on the compose network is safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
