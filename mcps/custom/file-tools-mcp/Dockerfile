# The base pin follows VERSIONS.md (PYTHON_IMAGE); the build overlay passes
# it as a build arg and a bare `docker build` falls back to the default.
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
FROM ${PYTHON_IMAGE}

# LibreOffice (Writer/Calc/Impress only — not the full metapackage)
# WeasyPrint system deps: libpango, libharfbuzz, libgdk-pixbuf
# libcairo2: cairosvg (equation SVG → PNG for pptx/xlsx)
# Fonts for consistent document rendering (Debian 13 ships Liberation Sans
# Narrow, which draws Arial Narrow, apart from fonts-liberation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    fonts-liberation fonts-liberation-sans-narrow fonts-dejavu-core fonts-comfortaa \
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

# fonts layer: begin
# The fonts documents are drawn and measured with (fonts.py): Carlito draws
# Calibri at the same widths, Noto Sans and Serif cover Greek, and
# LibreOffice's math module draws equations, all Debian packages built for
# arm64 too. Caladea draws Cambria: Debian's (and Google Fonts') Caladea is
# a later rebuild with other widths, so the image takes the one LibreOffice
# and Collabora ship (crosextrafonts 2013, Apache-2.0, the font files only),
# pinned by its sha256, its licence text beside it. Their own layer after
# tini, so every layer above stays shared with the images before it.
ARG CALADEA_URL=https://dev-www.libreoffice.org/src/368f114c078f94214a308a74c7e991bc-crosextrafonts-20130214.tar.gz
ARG CALADEA_SHA256=c48d1c2fd613c9c06c959c34da7b8388059e2408d2bb19845dc3ed35f76e4d09
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-crosextra-carlito fonts-noto-core libreoffice-math \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && python3 -c 'import hashlib, io, sys, tarfile, urllib.request; \
data = urllib.request.urlopen(sys.argv[1], timeout=120).read(); \
assert hashlib.sha256(data).hexdigest() == sys.argv[2], "the Caladea download does not match its sha256"; \
archive = tarfile.open(fileobj=io.BytesIO(data)); \
[archive.extract(m, "/tmp/caladea", filter="data") for m in archive.getmembers() if m.name.endswith(".ttf")]' \
       "$CALADEA_URL" "$CALADEA_SHA256" \
    && install -D -m 0644 -t /usr/local/share/fonts/caladea /tmp/caladea/*/Caladea-*.ttf \
    && install -m 0644 /usr/share/common-licenses/Apache-2.0 /usr/local/share/fonts/caladea/LICENSE \
    && rm -rf /tmp/caladea && fc-cache -f
# fonts layer: end

# numpy's OpenBLAS (which openpyxl, matplotlib and PIL import) reserves a
# buffer per CPU thread when it loads, address space each worker's memory
# cap counts; one thread is what a document job uses. OMP_NUM_THREADS stays
# unset: onnxruntime reads it, and one thread slows a background removal
# fourfold.
ENV OPENBLAS_NUM_THREADS=1

# The modules, their subfolders and data, and the notices (.dockerignore
# admits only those).
COPY . .

USER mcp
EXPOSE 8932
# OCI image metadata. The version is a build arg the release workflow and
# the build overlay pass from VERSIONS.md, declared after every RUN so a new
# version never invalidates a cached layer. The licenses label names
# OtoDock's own code; the third-party terms are in THIRD-PARTY-NOTICES.md
# with their texts in licenses/, and in each Debian package's
# /usr/share/doc/<package>/copyright.
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
