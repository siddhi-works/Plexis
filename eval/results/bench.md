# QED-Bench results

80 generated requests, 70 planned, 10 proven impossible.

| Metric | QED | Greedy | Filter-greedy |
|---|---|---|---|
| Requests answered with a plan that passes every check | 100.0% of planned | 13.8% | 25.0% |
| Impossible requests answered with a wrong plan | 0% (proven conflict instead) | 100.0% | 60.0% |
| Independent cross-check agrees (Z3 proof or arithmetic cost floor) | 100.0% | n/a | n/a |
| Impossible requests with verified fix-its | 100.0% | n/a | n/a |
| End-to-end latency p50 / p95 | 88.9 / 157.7 ms | n/a | n/a |
| Compiles within the 500 ms Alexa+ budget | 100.0% | n/a | n/a |

Valid = passes every check of the independent verifier on the same partner data. QED never returns an invalid plan by construction (it fails closed); for impossible requests it returns a proven-minimal conflict instead of a plan.
