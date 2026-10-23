# Product feedback

Feedback on each tool Plexis used, written for the teams who own them. The detailed timeline is in `FRICTION_LOG.md`.

## Alexa+ MCP Toolkit (docs; the toolkit itself is partner-only preview)

**Worked well**
- Building on plain MCP is the right call. Plexis is a standard Streamable HTTP server, and nothing in it is Alexa-specific apart from conventions.
- The functional requirements read like a real certification checklist, and we built against them directly:
  - explicit confirmation before payment and a price breakdown
  - duplicate-booking prevention and stable IDs
  - never read IDs aloud
  - spoken and on-screen content match

**Would help**
- Reconcile the 500 ms round-trip rule with the 3 s "search results" rule. State which one certification measures, and where.
- Publish which elicitation form schemas Alexa+ renders by voice and on screen: enums, booleans, numbers, dates.
- Fix the auth page inconsistencies: `refresh_token` appears in the example, and the scope is spelled both `mcp:resource` and `mcp:resources`. Also say when `WWW-Authenticate` on 401 will be supported, since the MCP spec requires it.
- Offer a hosted or local Alexa+ host emulator for builders without preview access. It could run the certification checks against a URL. We had to write our own.
- Document how an add-on reaches the customer proactively, for example to say a flight was cancelled. Is `notifications/resources/updated` honored? Is there an Alexa notifications path for MCP add-ons?

## MCP Python SDK (2.1.1)

- `MCPServer` with core MCP Apps (`Apps` extension), typed `CallToolResult` with `outputSchema` validation, and `ctx.elicit` with a pydantic schema all worked well.
- The `FastMCP` → `MCPServer` rename is well signposted. The stub module that raises with a migration link was genuinely helpful.
- Logging is deprecated in 2026-07-28, but it is the only unsolicited channel in 2025-11-25. A version-aware "notify the user" helper would avoid every server suppressing the warning.

## MCP TypeScript SDK (1.32.1)

- The client, Streamable HTTP transport and elicitation handlers work against the Python 2.x server with no changes.
- Please add a supported hook for observing raw JSON-RPC messages. Wrapping `transport.onmessage` breaks the connection, because the SDK assigns it before `start()`.

## Strands Agents SDK (1.62)

- `structured_output_model=` on the agent call makes typed extraction to pydantic short and clean.
- It pins `mcp<2.2`, which blocks the newest MCP SDK. A looser pin or an extra would help.
- When no AWS identity exists, a call waits through the whole credential chain, about 20 s here. A fast "no credentials" error, or a documented pre-check, would make graceful degradation easier.

## Amazon Bedrock (Nova), Bedrock Guardrails, AgentCore Runtime

- These are not exercised live; no AWS account was available. The integration is written against the documented APIs:
  - Strands `BedrockModel`
  - `ApplyGuardrail` with `source=INPUT`
  - AgentCore Runtime's `0.0.0.0:8000/mcp` container contract
- Please clarify how an AgentCore inbound JWT authorizer formats its 401. An add-on can't satisfy Alexa+'s "no `WWW-Authenticate`" rule if the platform answers first.

## OR-Tools CP-SAT and Z3

- CP-SAT's assumption literals plus `SufficientAssumptionsForInfeasibility` make minimal conflict explanations practical in under a second.
- It's easy to miss that `linearization_level=2` is the difference between a 10% optimality gap and a proof in 60 ms on assignment-style models. A note in the docs would save people time.
- Z3 times out on pseudo-Boolean budget-infeasibility proofs of about 1,500 terms with every tactic we tried. We added an arithmetic lower bound in front of it.
