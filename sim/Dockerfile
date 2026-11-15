# The simulated devices (concept section 8): python -m cave_sim. Only in the demo and in CI,
# never at home. Build from the repository root, which holds uv.lock:
#
#   docker build -f sim/Dockerfile -t cave-sim .
#
# sim/Dockerfile.dockerignore keeps the build context to what is copied below. The image installs
# the "sim" dependency group from uv.lock and nothing else: no Home Assistant, no pytest, and not
# cave_sim.testing, which is removed so that nothing at run time can rely on it.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/sim \
    PATH=/opt/venv/bin:$PATH \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --only-group sim --no-install-project --no-cache

COPY sim/cave_sim /app/sim/cave_sim
COPY sim/demo /app/sim/demo
RUN rm -rf /app/sim/cave_sim/testing \
    && useradd --system --no-create-home --uid 10001 cave-sim

USER cave-sim
EXPOSE 8480 8481
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8481/api/health', timeout=4)"]
CMD ["python", "-m", "cave_sim"]
