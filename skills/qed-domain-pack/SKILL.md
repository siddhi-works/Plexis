---
name: qed-domain-pack
description: Add a new verified-planning domain (for example events, errands or group dinners) to the Plexis intent compiler, or a new rule to the travel domain, with the parser, solver encoding, independent verifier check, Z3 encoding and tests kept in lockstep. Use when extending Plexis or when a request phrase is reported as "not understood" (QED-W001).
---

# Extending Plexis without breaking the proof

Plexis only ever speaks plans that three independent implementations agree on. Every new rule must therefore be added in **four** places, and never by reusing one implementation inside another.

## Checklist for a new rule kind

1. **IR** (`src/qed/ir.py`): add a `_C` subclass with a unique `kind` literal, typed fields and a plain-English `describe()`. Register it in `Constraint` and `CONSTRAINT_KINDS`.
2. **Parser** (`src/qed/compiler/rules.py`): add a regex rule that calls `st.add(Constraint(...), match)` so the span records the exact words. Add the phrasing to `tests/test_parser.py`.
3. **CP-SAT encoding** (`src/qed/solver/cpsat.py`, `TripModel._encode`): encode it guarded by its assumption literal (`OnlyEnforceIf(lit)`). Prefer "sum of disallowed choices == 0" for filters and `>= n` for counts.
4. **Independent verifier** (`src/qed/verify/checker.py`, `_check_rule`): recompute it from the concrete plan, with evidence text a person can read. Do not import solver code.
5. **Z3 encoding** (`src/qed/solver/z3check.py`, `_Enc.rule`): encode it again with pseudo-Boolean constraints.
6. **Fix-its** (optional, `src/qed/solver/diagnose.py`, `_relaxation`): if the rule has a numeric bound, return the tightest value that works with everything else.
7. **Tests**: add it to the `rule` strategy in `tests/test_properties.py` so Hypothesis checks soundness, minimality and monotonicity, and add a mutation test in `tests/test_verifier.py` proving the checker catches a violation.

Run `pytest -q` and `qed bench --n 40`. The bench must still report 100% valid plans and 0% wrong plans on impossible requests.

## Checklist for a new domain

- Model the domain's catalog records in `src/qed/domain/models.py` (structured fields only; free text is display-only and untrusted).
- Serve them from a partner MCP server in `src/qed/partners/servers.py` with read tools annotated `read_only_hint=True`.
- Describe the schedule (which slots must be filled) next to `src/qed/solver/schedule.py`.
- Keep the same compile pipeline: parse, ground, solve, verify, cross-check, sign. Never let partner text reach decision logic.

## Rules that are not negotiable

- An LLM may propose constraints, but each must quote the request verbatim (`guard.check_llm_provenance`).
- Never present a plan that fails verification; fail closed.
- Anything spoken must be derivable from the signed certificate (`guard.grounded`).
