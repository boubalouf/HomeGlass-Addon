ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Python 3.12 est fourni par Alpine 3.20 (images de base Home Assistant).
RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    HG_WEB_DIR=/opt/homeglass/web \
    HG_SCHEMA_DIR=/opt/homeglass/schema

WORKDIR /opt/homeglass

# Dépendances d'abord : la couche est réutilisée tant que pyproject.toml ne change pas.
COPY pyproject.toml ./
COPY homeglass_server ./homeglass_server
RUN pip install .

# Contenu servi par l'add-on (interface web, schéma JSON versionné).
COPY web ./web
COPY schema ./schema

COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8099 8765

CMD ["/run.sh"]
