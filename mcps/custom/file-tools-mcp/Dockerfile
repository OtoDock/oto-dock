# The base pin follows VERSIONS.md (PYTHON_IMAGE); the build overlay passes
# it as a build arg and a bare `docker build` falls back to the default.
ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm
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
# any network at runtime). isnet-general-use = cleaner edges/hair than the
# default u2net at the same ~176MB / CPU-speed class. Must match
# images.py::_REMBG_MODEL.
ENV U2NET_HOME=/app/models
RUN python3 -c "from rembg import new_session; new_session('isnet-general-use')" \
    && chmod -R a+r /app/models

COPY *.py .

USER mcp
EXPOSE 8932
CMD ["python", "server.py"]
