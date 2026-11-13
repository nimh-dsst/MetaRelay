# The fake LabArchives API server, for the docker compose dev stack.
# Build from the repository root: docker compose build fake-labarchives
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY docker/fake-labarchives/seed.json /seed/seed.json

ENV FAKE_LABARCHIVES_HOST=0.0.0.0 \
    FAKE_LABARCHIVES_PORT=8080 \
    FAKE_LABARCHIVES_SEED=/seed/seed.json \
    FAKE_LABARCHIVES_LOG=1
EXPOSE 8080
USER nobody
CMD ["python", "-m", "metarelay.testing.labarchives_fake"]
