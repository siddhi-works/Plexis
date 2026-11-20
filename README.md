# Plexis

[![CI](https://github.com/siddhi-works/Plexis/actions/workflows/ci.yml/badge.svg)](https://github.com/siddhi-works/Plexis/actions/workflows/ci.yml) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue) ![MCP 2025-11-25](https://img.shields.io/badge/MCP-2025--11--25-6366f1) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A trip-planning agent where the language model never decides the trip.**

Most AI travel agents let a language model write the itinerary and ask you to double-check it. Plexis compiles your request into exact rules, uses a constraint solver to find a plan that satisfies all of them, and has an independent checker verify the result before anything is said or booked. If the request is impossible, it names the rules that clash and the smallest change that fixes it. Every plan comes with a signed, tamper-evident receipt.

Built as an Alexa+ add-on (a self-hosted MCP server) for Amazon's *Build, Ship, Shape* Developer Hackathon.

<p align="center"><img src="docs/media/plexis-demo.gif" width="420" alt="Plexis demo"></p>

<p align="center"><a href="docs/media/plexis-demo.mp4">Watch the launch video (1:12)</a></p>

## Highlights

- **Solved, not generated.** OR-Tools CP-SAT finds the best plan that satisfies every rule, or proves none exists.
- **Independently verified.** A separate checker recomputes every rule, and Z3 cross-checks the result.
- **Explains impossibility.** It extracts the minimal set of clashing rules and offers fixes that are re-solved to prove they work.
- **Signed receipts.** Each plan is recorded in an Ed25519-signed, SHA-256 hash-chained ledger.
- **Conversational.** It asks only for what's missing and remembers household needs, per user and with consent.
- **Alexa+ native.** An MCP server (protocol 2025-11-25, Streamable HTTP) with structured results, elicitation and two-tier OAuth.

## Architecture

![How Plexis turns a request into a proven plan](docs/media/architecture.png)

The full stage-by-stage design (constraint compilation, the CP-SAT encoding, conflict extraction, verification and the receipt ledger) is in [docs/architecture.md](docs/architecture.md).

## Results

80 seeded requests (70 solvable, 10 provably impossible), judged by the independent verifier on the same partner data. Reproduce with `plexis bench --n 80`. [Full results](docs/benchmark/bench.md).

| | Plexis | Greedy | Filter-greedy |
|---|---|---|---|
| Plans that pass every check | **100%** | 13.8% | 25.0% |
| Impossible requests answered with a wrong plan | **0%** | 100% | 60.0% |
| Impossible requests that come with verified fixes | 100% | n/a | n/a |
| Latency p50 / p95 | 89 / 158 ms | n/a | n/a |

![The Proof view: every rule checked and linked to the words that asked for it, with the receipt ledger verified in the browser](docs/media/plexis-proof.png)

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and Node.js 20+.

```bash
uv venv && uv pip install -e ".[dev]"
plexis dev                                   # the add-on and its partner services
npm --prefix simulator install
npm --prefix simulator run dev               # simulated Alexa+ host at http://localhost:5173
```

The simulator is a real MCP client. Its console shows the protocol trace, latency, the proof, and a chaos lab for injecting partner failures.

```bash
pytest -q                                    # 105 tests
uv pip install -e ".[voice]" && plexis voice # hands-free voice client
```

## Project structure

```
src/plexis/
  compiler/     request -> typed rules
  solver/       CP-SAT model, conflict diagnosis, Z3 cross-check
  verify/       independent plan checker
  receipts/     signed, hash-chained ledger
  agent/        dialog and learning
  partners/     partner MCP servers and resilient client
  server/       MCP server, OAuth, result shaping
  device/       voice clients
  engine.py     the pipeline that ties it together
simulator/      simulated Alexa+ host (React, MCP TypeScript SDK)
tests/          unit, property, contract, chaos, privacy and timing tests
deploy/         Alexa+ add-on manifest and AWS AgentCore guide
docs/           architecture, benchmark results and media
```

## Validation & Integration Scope

Plexis is a functional prototype with independently tested planning, constraint verification, and cryptographically signed receipts.

- **Travel services:** Eight partner MCP servers simulate travel and payment workflows using reproducible datasets. The architecture supports integration with live provider APIs.
- **Alexa+ compatibility:** Built around MCP and Alexa+ add-on conventions, with functionality demonstrated through an MCP-compatible simulator. Native Alexa+ deployment requires partner access.
- **AWS services:** Amazon Bedrock and AgentCore integration paths are implemented with graceful fallbacks. Live cloud validation remains outside the current evaluation scope.

The published benchmarks measure the core planning and verification system under controlled conditions, independently of external service availability.

## License

[MIT](LICENSE)
