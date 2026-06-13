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
COPY pygal-tooltips.min.js .

# config.json is bind-mounted at runtime — never bake credentials into the image.
# chaos.log is bind-mounted at runtime so logs are accessible on the host.

EXPOSE 8087

# `touch` ensures chaos.log exists as a file before Python opens it as a FileHandler.
# On Linux, bind-mounting a file that doesn't exist on the host causes Docker to create
# a directory there instead — which makes Python's FileHandler crash with IsADirectoryError.
# `exec` replaces sh with Python so the process remains PID 1 and receives SIGTERM cleanly.
CMD ["sh", "-c", "touch /app/chaos.log && exec python chaos.py"]
