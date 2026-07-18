# Example job container for rotate-local-admin.
# Build:   docker build -t registry.example.com/jobs/rotate-admin:1.4.0 .
# Publish: remote-jobs publish --image registry.example.com/jobs/rotate-admin:1.4.0
FROM python:3.11-slim

# The worker agent runs job containers read-only, non-root, with tmpfs /tmp
# (SPEC 12.3); keep the image compatible with that.
RUN useradd --uid 1000 --create-home jobrunner

WORKDIR /app
RUN pip install --no-cache-dir nautobot-remote-jobs-sdk

COPY job.py /app/job.py

# Embed the manifest so `remote-jobs publish` can also read it from the
# image label in the future (SPEC 14).
COPY remote-job.yaml /app/remote-job.yaml
LABEL com.nautobot.remote-jobs.manifest="/app/remote-job.yaml"

USER jobrunner
ENTRYPOINT ["python", "/app/job.py"]
