# ============================================================================
# Stage 1: Build frontend
# ============================================================================
FROM node:22-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS frontend-build
# node:22-slim digest resolved 2026-07-28

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ============================================================================
# Stage 2: Python builder — compiles wheels + builds a self-contained venv.
# build-essential lives ONLY here; it never reaches the runtime image.
# ============================================================================
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS builder
# python:3.11-slim digest resolved 2026-07-13

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

COPY agent/requirements.txt agent/requirements.txt
COPY requirements-lock.txt requirements-lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-lock.txt

# Authoritative XNYS calendar supplement. The main lock already supplies numpy,
# pandas and tzdata. --no-deps prevents silent dependency expansion.
COPY agent/requirements-market-calendar.txt agent/requirements-market-calendar.txt
COPY requirements-market-calendar-lock.txt requirements-market-calendar-lock.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements-market-calendar-lock.txt \
    && python -c "import exchange_calendars as xcals; assert 'XNYS' in xcals.get_calendar_names()"

COPY requirements-channels-lock.txt requirements-channels-lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-channels-lock.txt

COPY pyproject.toml LICENSE README.md ./
COPY agent/ agent/
RUN pip install --no-cache-dir --no-deps -e .

# ============================================================================
# Stage 3: Runtime — carries the prebuilt venv only, no compilers/dev headers.
# ============================================================================
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS runtime
# python:3.11-slim digest resolved 2026-07-13

LABEL org.opencontainers.image.title="Vibe-Trading" \
    org.opencontainers.image.description="Natural-language finance research AI agent with backtesting" \
    org.opencontainers.image.version="0.1.14" \
    org.opencontainers.image.source="https://github.com/HKUDS/Vibe-Trading" \
    org.opencontainers.image.licenses="MIT"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv

# Re-materialize the source tree the editable install references and include the
# operational scripts used by the supervised Trading Desk worker.
COPY pyproject.toml LICENSE README.md ./
COPY agent/ agent/
COPY scripts/ scripts/
COPY --from=frontend-build /app/frontend/dist frontend/dist

RUN useradd --create-home --shell /usr/sbin/nologin vibe \
    && useradd --system --no-create-home --shell /usr/sbin/nologin --uid 10001 vibe-sandbox \
    && mkdir -p agent/runs agent/sessions agent/uploads agent/.swarm/runs /home/vibe/.vibe-trading /app/data \
    && chown -R vibe:vibe /app /home/vibe/.vibe-trading
USER vibe

EXPOSE 8899

# PORT-aware liveness probe so the same image works on Render and locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/live' % os.environ.get('PORT','8899'))" || exit 1

# Default image behavior remains the web/API server. Hosting blueprints can
# override this with scripts/start_trading_platform.py to run web + worker.
CMD ["sh", "-c", "vibe-trading serve --host 0.0.0.0 --port ${PORT:-8899}"]
