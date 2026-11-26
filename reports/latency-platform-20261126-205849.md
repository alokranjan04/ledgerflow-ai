# Latency — platform — 2026-11-26 20:58 — target http://localhost:8091

| Concurrent sessions | Call | n | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| 25 | login | 25 | 43 | 119 | 213 |
| 25 | account | 25 | 281 | 684 | 838 |
| 25 | activity | 25 | 280 | 789 | 856 |
| 25 | errors | 0 | 0 | 0 | 0 |
| 50 | login | 50 | 71 | 156 | 365 |
| 50 | account | 50 | 278 | 572 | 613 |
| 50 | activity | 50 | 291 | 693 | 1077 |
| 50 | errors | 0 | 0 | 0 | 0 |
| 100 | login | 100 | 99 | 229 | 323 |
| 100 | account | 100 | 394 | 672 | 913 |
| 100 | activity | 100 | 265 | 631 | 737 |
| 100 | errors | 0 | 0 | 0 | 0 |
| 200 | login | 200 | 164 | 339 | 427 |
| 200 | account | 200 | 843 | 1127 | 1260 |
| 200 | activity | 200 | 308 | 699 | 845 |
| 200 | errors | 0 | 0 | 0 | 0 |

Server-side connector latency (includes simulated system latency, retries):

| Metric | n | p50 | p95 | p99 |
|---|---|---|---|---|
| connector.cards | 375 | 211.4 | 555.6 | 684.4 |
| connector.crm | 375 | 102.9 | 289.0 | 363.6 |
| connector.loans | 750 | 243.8 | 620.2 | 775.4 |
| connector.ops | 375 | 288.7 | 691.6 | 844.4 |
| connector.policy | 1 | 63.0 | 63.0 | 63.0 |
| tool.get_card_summary | 375 | 211.4 | 555.6 | 684.4 |
| tool.get_loan_summary | 375 | 521.1 | 1040.6 | 1175.0 |
| tool.list_service_requests | 375 | 288.8 | 691.7 | 844.4 |
