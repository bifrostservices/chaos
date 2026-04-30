FROM python:3.13-slim

# git is required to install python-lucidmotors directly from GitHub
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying the app so this layer is cached
# as long as requirements.txt doesn't change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chaos.py .

# config.json is bind-mounted at runtime — never bake credentials into the image.
# chaos.log is bind-mounted at runtime so logs are accessible on the host.

EXPOSE 8086

# Run as PID 1 so SIGTERM (docker stop) reaches the process and triggers clean shutdown.
CMD ["python", "chaos.py"]
