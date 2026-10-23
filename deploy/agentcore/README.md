# Deploying Plexis to Amazon Bedrock AgentCore Runtime

> Status: the container is built to AgentCore Runtime's MCP contract, but this
> build has **not** been deployed to AgentCore (no AWS account was available
> during the hackathon). The steps below are the intended path.

AgentCore Runtime hosts MCP servers that listen on `0.0.0.0:8000/mcp`, which is
exactly what the `Dockerfile` produces (`qed serve`).

## Why stateful mode

Plexis uses three server-to-client features from MCP 2025-11-25: **elicitation**
(consent before payment, fix-it choices), **progress notifications** (voice
"working on it" updates) and **resource-updated / log notifications** (proactive
cancellation alerts). These need a stateful Streamable HTTP session, so
configure the runtime in stateful MCP mode.

## Steps

1. Build and push the image to Amazon ECR:
   ```bash
   docker build -t qed .
   ```
2. Create the AgentCore Runtime with the MCP protocol, stateful sessions, and
   an inbound JWT authorizer. AgentCore Identity or Login with Amazon can
   issue the tokens; Plexis's verifier only needs the issuer, audience
   (`https://<runtime-url>/mcp`) and the scopes `mcp:service` / `mcp:tools`.
3. Set environment variables: `QED_PUBLIC_URL`, `QED_PARTNERS_URL` (or AgentCore
   Gateway targets for real partner APIs), `QED_LLM=bedrock`, `AWS_REGION`,
   `QED_GUARDRAIL_ID`, and `QED_BACKUP_S3_BUCKET`.
4. Point the Alexa+ add-on manifest (`alexa/addon-package/addon.json`) at the
   runtime URL and run `alexa-ai deploy`.

## Known gap

The Alexa+ quickstart expects unauthenticated requests to get a 401 *without* a
`WWW-Authenticate` header, while the MCP spec requires one. Plexis strips it when
`QED_ALEXA_COMPAT_401=true` (the container default). If an AgentCore inbound
authorizer answers the 401 before Plexis sees the request, Plexis can't strip the
header. Check how the runtime responds before submitting (unverified, see
`FRICTION_LOG.md`).
