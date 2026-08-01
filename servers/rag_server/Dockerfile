# rag_server - vector search MCP server.
#
# Served over streamable HTTP under compose: stdio cannot cross a container
# boundary (see docs/design.md section 6). The same image still supports stdio
# via --transport stdio for local, non-containerised use.
FROM python:3.12-slim

WORKDIR /app

# --no-cache-dir: the pip cache would otherwise persist in the image layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY retrieval.py server.py ./

# Bake the embedding weights into the image at build time. Fetching them lazily
# on the first query meant the container reached out to huggingface.co while
# serving a request - runtime network egress, which HLD.md section 7 forbids -
# and the cold start was slow enough that the first tool call timed out before
# the model finished loading. Downloading here makes the running container
# genuinely air-gapped and costs ~80 MB.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')"

# ./data is bind-mounted here by compose (bind mounts only, no named volumes -
# HLD.md section 5).
ENV RAW_DOCS_DIR=/app/data/raw_docs \
    CHROMA_DIR=/app/data/chroma_db \
    ANONYMIZED_TELEMETRY=False \
    MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8765

EXPOSE 8765

CMD ["python", "server.py"]
