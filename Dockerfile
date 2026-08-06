# The stdio bridge for clients and sandboxes that launch a command instead of
# calling an HTTPS endpoint. No dependencies, so nothing is installed.
FROM python:3.12-slim

WORKDIR /app
COPY mcp_stdio.py validate.py ./

RUN useradd --create-home --uid 10001 mcp
USER mcp

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "mcp_stdio.py"]
