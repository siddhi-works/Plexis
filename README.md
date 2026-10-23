# Plexis: Alexa+ trips you can trust

Plexis is an Alexa+ add-on: a self-hosted MCP server (protocol 2025-11-25, Streamable HTTP) that compiles a spoken trip request into a plan that comes with a proof. Every plan goes through four steps:
- It is optimized by OR-Tools CP-SAT.
- An independent verifier re-checks it.
- Z3, or an arithmetic cost floor, cross-checks it.
- It is signed with Ed25519 into a hash-chained ledger.

If a request is impossible, Plexis names the minimal set of clashing requirements and offers verified fix-its.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
qed dev
```

Then start the simulator:

```bash
npm --prefix simulator install && npm --prefix simulator run dev
```

Open http://localhost:5173 to use the simulated Alexa+ host. It is a real MCP client (TypeScript SDK), and its console has four tabs: protocol trace, latency, proof and chaos lab.

Voice-only device (no browser): `qed voice` (add `--text` to type). Install the extra with `uv pip install -e ".[voice]"`.

## Results (`qed bench --n 80`, seeded, independent verifier as judge)

| | Plexis | Greedy | Filter-greedy |
|---|---|---|---|
| Plans passing every check | 100% | 13.8% | 25.0% |
| Wrong plan returned for an impossible request | 0% | 100% | 60.0% |
| Independent cross-check agrees (Z3 or cost floor) | 100% | | |
| Latency p50 / p95 | 89 / 158 ms | | |

## What's inside

- **Partner servers:** 7 partner MCP servers covering transport, stays, dining, experiences, weather, wallet and a simulated front desk.
- **Alexa+ add-on conventions:**
  - `structuredContent` with an `outputSchema`, plus `_meta.ui` pointing at an MCP App card for Echo Show.
  - Elicitation asks for consent before payment and for the user's fix-it choice.
  - Duplicate bookings are prevented.
  - Two-tier OAuth: `client_credentials` for discovery, and `authorization_code` with PKCE for personal actions.
  - An `addon.json` manifest in `alexa/addon-package/`.
- **Household memory:** saved only with consent, and every fact that shapes a plan is labelled in it.
- **Weather:** the plan is weather-aware and can suggest better dates.
- **Booking:** a saga with rollback.
- **Cancellations:** Plexis re-plans automatically and pushes an MCP notification.
- **Travel brief:** what to pack and wear, with general (non-medical) comfort notes.
- **Guardrails:** checks on input, on LLM provenance, on untrusted partner text and on spoken output.
- **Resilience:** circuit breakers, retries, a stale cache and bulkheads.
- **Observability and safety nets:** Prometheus metrics, OpenTelemetry tracing, and ledger backup and restore.
- **AWS:**
  - **Amazon Bedrock:** Nova models via the Strands Agents SDK, plus Bedrock Guardrails. They are optional and degrade cleanly without credentials.
  - **Amazon S3:** ledger backups.
  - **AgentCore Runtime:** deploy artifacts are in `deploy/agentcore/`. They have not been deployed.
- **Agent Skill:** `skills/qed-domain-pack/`.

## Tests

`pytest -q`: 90 tests covering unit, Hypothesis property and metamorphic checks, verifier mutation, live MCP contract and chaos scenarios, plus a latency SLO.

## Honest limits

- All partners are simulated, with seeded and fictional data.
- Real Alexa+ access is partner-only, so the simulator stands in for Alexa+.
- Bedrock and AgentCore have not been exercised live.

## More

- [SUBMISSION.md](SUBMISSION.md): the Devpost text
- [DEMO_SCRIPT.md](DEMO_SCRIPT.md): the video script
- [FRICTION_LOG.md](FRICTION_LOG.md) and [PRODUCT_FEEDBACK.md](PRODUCT_FEEDBACK.md)
- [LICENSE](LICENSE): MIT
