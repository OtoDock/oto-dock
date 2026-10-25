# The base pin follows VERSIONS.md (PYTHON_IMAGE); the build overlay passes
# it as a build arg and a bare `docker build` falls back to the default.
ARG PYTHON_IMAGE=python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
FROM ${PYTHON_IMAGE}

# LibreOffice (Writer/Calc/Impress only — not the full metapackage)
# WeasyPrint system deps: libpango, libharfbuzz, libgdk-pixbuf
# libcairo2: cairosvg (equation SVG → PNG for pptx/xlsx)
# Fonts for consistent document rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    fonts-liberation fonts-dejavu-core fonts-comfortaa \
    libpango-1.0-0 libharfbuzz0b libffi-dev libgdk-pixbuf-2.0-0 \
    libcairo2 \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-ell \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# PyMuPDF's OCR finds the language data here; unset, it runs `tesseract
# --list-langs` to look for it on every OCR call. The path is the Debian
# package's for tesseract 5.
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

RUN useradd -m -s /bin/bash mcp

WORKDIR /app
COPY requirements.txt .
# The headless browser of the app checks (render.py, APPS.md "Deploy
# pipeline"): Chromium's headless shell, installed system-wide before USER
# mcp so the runtime user can read it; --with-deps pulls the browser's
# shared libraries through apt.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium-headless-shell \
    && chmod -R a+rX /opt/ms-playwright \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Pre-download the rembg background-removal model (avoids first-use delay +
# any network at runtime). isnet-general-use = cleaner edges/hair than u2net
# at the same ~176MB / CPU-speed class. Must match images.py::_REMBG_MODEL.
# U2NET_HOME is the model home rembg reads (under models/<name>/).
ENV U2NET_HOME=/app/models
RUN python3 -c "from rembg import new_session; new_session('isnet-general-use')" \
    && chmod -R a+r /app/models

# An init as PID 1: LibreOffice leaves helper processes to its parent, and
# the server never reaps them; tini reaps every orphan and passes the stop
# signal on. Its own layer, so the layers above stay cached.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY *.py .

USER mcp
EXPOSE 8932
# OCI image metadata. The version is a build arg the release workflow and
# the build overlay pass from VERSIONS.md, declared after every RUN so a new
# version never invalidates a cached layer.
ARG OTODOCK_VERSION=dev
LABEL org.opencontainers.image.title="OtoDock file tools" \
      org.opencontainers.image.description="The OtoDock file tools: documents, PDFs, images and conversions for the agents." \
      org.opencontainers.image.source="https://github.com/OtoDock/oto-dock" \
      org.opencontainers.image.url="https://otodock.io" \
      org.opencontainers.image.documentation="https://docs.otodock.io" \
      org.opencontainers.image.vendor="OtoDock" \
      org.opencontainers.image.licenses="FSL-1.1-ALv2" \
      org.opencontainers.image.version="${OTODOCK_VERSION}"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]
