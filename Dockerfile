FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# git is required to install python-lucidmotors directly from GitHub
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying the app so this layer is cached
# as long as pyproject.toml/uv.lock don't change.
COPY pyproject.toml uv.lock .
RUN uv sync --locked --no-dev --no-install-project

COPY chaos.py .
COPY pygal-tooltips.min.js .

# config.json is bind-mounted at runtime — never bake credentials into the image.
# data/ (chaos.log + long-history JSON) is bind-mounted at runtime for persistence.

EXPOSE 8087

# `exec` replaces sh with Python so the process remains PID 1 and receives SIGTERM cleanly.
CMD ["sh", "-c", "exec uv run --no-dev python chaos.py"]
