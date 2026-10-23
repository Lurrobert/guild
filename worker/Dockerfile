# Guild worker image (optional): runs guild_worker.py, which starts each job in its own sandboxed container.
# The worker needs the host's Docker socket to start job containers; the jobs themselves get no network.
# Build it yourself if you want it; Guild publishes no image.
FROM python:3.12-slim
RUN pip install --no-cache-dir cryptography==44.0.2 \
 && apt-get update && apt-get install -y --no-install-recommends docker.io tar \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY guild_worker.py /app/guild_worker.py
COPY guild_client.py /app/guild_client.py
ENTRYPOINT ["python3", "/app/guild_worker.py"]
