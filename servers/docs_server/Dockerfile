# docs_server - PDF parsing and OCR MCP server (Phase 4).
#
# This server is containerised ahead of Phase 6 out of necessity, not
# preference: pytesseract needs the Tesseract binary, and installing it on
# Windows would write to C:\Program Files, which resource-governance.md
# section 1 forbids. Inside this image it lands in the DockerEngine distro's
# vhdx, which lives on M:\ by construction.
FROM python:3.12-slim

# tesseract-ocr : the OCR engine pytesseract shells out to
# poppler-utils : pdftoppm, used by pdf2image to rasterise PDF pages
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --no-cache-dir: the pip cache would otherwise persist in the image layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# ./data is bind-mounted here by docker-compose (bind mounts only, no named
# volumes - see HLD.md section 5).
ENV DOCS_DIR=/app/data/raw_docs

# stdio transport: the MCP host attaches to this process's stdin/stdout.
CMD ["python", "server.py"]
