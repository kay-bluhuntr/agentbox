# --- build stage -------------------------------------------------------------
FROM python:3.13-slim-bookworm AS build

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentbox ./agentbox
# Refresh OS packages to pick up security fixes before installing Python deps
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --prefix=/install .

# --- runtime stage ------------------------------------------------------------
FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN groupadd -r agentbox && useradd -r -g agentbox -u 10001 agentbox

COPY --from=build /install /usr/local
COPY agentbox /app/agentbox

RUN chmod -R a+rX /app/agentbox

WORKDIR /app
USER 10001

EXPOSE 8080
ENTRYPOINT ["uvicorn", "agentbox.main:app", "--host", "0.0.0.0", "--port", "8080"]
