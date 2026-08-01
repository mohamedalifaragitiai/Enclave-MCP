# agent - LangGraph MCP host.
#
# Run as a one-shot rather than a long-lived service; it answers a question and
# exits:
#
#   docker compose run --rm agent "What does OSHA require for mechanical integrity?"
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY host.py .

# LangSmith ships with LangChain and would emit network traffic; HLD.md
# section 7 requires none. host.py also sets these defensively at import time.
ENV LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false

ENTRYPOINT ["python", "host.py"]
