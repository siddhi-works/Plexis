# Devpost submission: Plexis

**Tracks:** Alexa+ (self-hosted MCP server, plus an Agent Skill) · Open Source mini challenge · AWS Builder mini challenge

**Tagline:** Alexa+ trips you can trust. Every plan comes with a proof, and every impossible request comes with the fix.

## Inspiration

Ask any assistant to plan a family trip with a budget, a wheelchair user and a vegetarian, and you get a confident itinerary. Then you find out:
- the hotel has two steps at the entrance;
- the "Thai dinner" is on the night the restaurant is closed;
- the total is $400 over.

People stop trusting an assistant after one bad booking. A voice assistant that spends real money has to be *right*, and it has to say so when what you asked for can't be done.

## What it does

Plexis is an Alexa+ add-on: a self-hosted MCP server that treats a spoken request like source code. It **compiles** the request into a plan that comes with a machine-checkable proof.

- **Plans you can prove.**
  - It finds the best trip: flights, trains or driving, plus lodging, every meal and activities.
  - An independent checker re-proves every requirement, and a second solver cross-checks it.
  - Everything is signed into a tamper-evident receipt.
  - The Echo Show card shows each rule next to the exact words you said that created it.
- **Impossible requests get a compiler error, not a hallucination.** "These can't all hold together: total under $1,400; hotel rated 4.7+." Plexis then offers the smallest change that works ("raise the budget by $41"), and it re-solves each fix before offering it.
- **"Why not the Harbor Hotel?"** It answers by forcing that choice and showing exactly which of your rules it would break.
- **A family member, not a form.**
  - Household memory is saved with consent and can be erased by voice. "Grandma uses a wheelchair" quietly makes every future plan step-free, and the plan says why.
  - It learns what the household enjoys.
- **It keeps watching after you book.**
  - If a booked flight is cancelled, Plexis re-plans around it (same hotel, every rule still proven) and alerts you proactively.
  - If the forecast turns wet, it compares nearby dates.
- **The practical stuff.**
  - A day-by-day outfit and packing brief from the forecast.
  - General comfort notes such as altitude, heat and flu season, with official sources (not medical advice).
  - A simulated, AI-disclosed call to the hotel front desk to ask about early check-in or a roll-in shower.

## How we built it

- **Compiler pipeline:**
  1. A deterministic parser records the exact span behind every constraint.
  2. Optionally, the request escalates to Amazon Bedrock (Nova via the Strands Agents SDK), which must quote your words or its proposal is dropped.
  3. Data is fetched in parallel from 7 partner MCP servers.
  4. OR-Tools CP-SAT solves the plan, with one assumption literal per rule.
  5. An independent verifier re-checks the result.
  6. Z3 cross-checks it, or an arithmetic cost floor does.
  7. The result is signed with Ed25519 into a hash-chained SQLite ledger, with backups (optionally to Amazon S3).
- **Impossible requests:** minimal unsatisfiable subsets are extracted from CP-SAT's infeasibility core, and fix-its come from optimizing each relaxed bound and re-solving.
- **Alexa+ conventions:**
  - It speaks MCP 2025-11-25 over Streamable HTTP.
  - Tools declare `outputSchema` and return `structuredContent`, with `_meta.ui` pointing at an MCP App card.
  - Elicitation confirms payment and offers fix-it choices.
  - Two-tier OAuth: `mcp:service` via `client_credentials`, and `mcp:tools` via authorization code with PKCE and RFC 8707 resource binding.
  - No IDs are spoken.
  - Booking is a saga with rollback and duplicate prevention.
- **Simulated Alexa+ host:** a React app that is a real MCP client (TypeScript SDK). It has voice input and output, an Echo Show screen and voice-only mode, and a console with four tabs:
  - JSON-RPC trace
  - latency waterfall against the 500 ms budget
  - the ledger, re-verified in the browser with WebCrypto and Ed25519
  - a chaos lab that breaks partners or cancels flights
- **Agent Skill** (`skills/qed-domain-pack`): teaches a coding agent to add new verified rules or domains, keeping all three implementations in lockstep.

## Results

On QED-Bench (80 generated requests, with the independent verifier as the judge):

| | Plexis | Greedy planner | Filter-then-greedy |
|---|---|---|---|
| Plans that pass every check | **100%** | 14% | 25% |
| Wrong plan returned for an impossible request | **0%** | 100% | 60% |
| Independent cross-check agrees | **100%** | n/a | n/a |
| Latency p50 / p95 | **89 / 158 ms** | | |

The test suite (`pytest`) has 90 tests:
- unit tests
- Hypothesis property and metamorphic tests (adding a rule never improves the optimum; raising a budget never breaks feasibility)
- mutation tests on the verifier
- live MCP contract tests over HTTP
- chaos tests for retries, circuit breakers and stale-cache degradation

## What's new in the final build

- **A real conversation, entirely by voice.** `qed voice` is a voice-only Alexa+-style device (press-to-talk, offline Whisper speech recognition, spoken replies) that talks to the add-on over MCP. Plexis asks one question at a time (where, when, who, from where, budget priced from live quotes, room needs) with three spoken suggestions, and keeps context across turns ("the first one", "3", "yes").
- **About 30 researched preferences**: pool, spa, gym, breakfast included, half board, all-inclusive, free parking, Wi-Fi, kitchen, view, quiet room, crib, family room, free cancellation, AC or no AC, shared or private room. Each is wanted or unwanted ("no pool"), and each is solved, verified and cross-checked.
- **Learning**: phrases you teach ("when I say beach vibes, I mean beachfront with a pool"), gaps it asks about, corrections it logs, and habits (asking for cheaper options makes value the default). Visible and forgettable.
- **LangGraph** runs every turn and every task graph: seven partner and research lookups in parallel, then solving, then verification and the cross-check side by side. Partner data is prefetched while the customer is still answering.
- **Real data where free**: Open-Meteo forecasts within 15 days; curated landmarks plus Wikivoyage listings for "don't miss" suggestions.
- **Audit fixes**: groups never get oversold flights, rental cars are always driven back, arrival times use local time zones, "no hostel" means private, "2 adults and 2 kids" counts 4, and conversations survive a server restart.

## Challenges

Details are in `FRICTION_LOG.md`:
- The MCP Toolkit is in partner-only preview.
- The Python and TypeScript MCP SDKs are on different major versions.
- Z3 can't prove tight budget conflicts in time, so we added an arithmetic certificate.
- The Alexa+ 401 rule conflicts with the MCP spec.

## What's next

1. Deploy to AgentCore Runtime in stateful mode.
2. Run live against Bedrock.
3. Connect real partner APIs through AgentCore Gateway.
4. Bring the same compiler to other domains: group dinners, errands, event tickets.

## Honest limits

- Partners and their data are simulated, seeded and fictional, and the front-desk call is simulated.
- We had no Alexa+ device or AWS account, so the simulator stands in for Alexa+, and Bedrock and AgentCore are wired up but not run live.
- `addon.json` has not been validated by `alexa-ai deploy`.

## Links

- **Repository:** (add the GitHub URL)
- **Video:** (add the URL)
- **Product feedback:** `PRODUCT_FEEDBACK.md`
- **Friction log:** `FRICTION_LOG.md`
