# Plexis MCP server image. Listens on 0.0.0.0:8000/mcp, the contract expected by
# Amazon Bedrock AgentCore Runtime for MCP servers. Elicitation, progress and
# disruption notifications need a *stateful* Streamable HTTP session, so deploy
# AgentCore Runtime in stateful MCP mode (see deploy/agentcore/README.md).
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .
RUN useradd --create-home --uid 10001 qed && mkdir -p /app/var && chown -R qed /app
USER qed
# Alexa+ quickstart: unauthenticated requests get a 401 *without* WWW-Authenticate (not supported yet).
ENV QED_HOST=0.0.0.0 QED_PORT=8000 QED_STATE_DIR=/app/var QED_DEV_MODE=false QED_ALEXA_COMPAT_401=true
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["qed", "serve"]
