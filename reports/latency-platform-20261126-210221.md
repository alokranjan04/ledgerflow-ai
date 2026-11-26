# Latency — platform — 2026-11-26 21:02 — target https://ledgerflow-ai-vccwx7nvsa-el.a.run.app

| Concurrent sessions | Call | n | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| 10 | login | 10 | 410 | 852 | 852 |
| 10 | account | 10 | 261 | 605 | 605 |
| 10 | activity | 10 | 283 | 746 | 746 |
| 10 | errors | 0 | 0 | 0 | 0 |
| 50 | login | 50 | 512 | 1374 | 1452 |
| 50 | account | 50 | 321 | 740 | 1070 |
| 50 | activity | 50 | 303 | 967 | 1125 |
| 50 | errors | 0 | 0 | 0 | 0 |
| 100 | login | 100 | 228 | 1318 | 1600 |
| 100 | account | 100 | 356 | 617 | 777 |
| 100 | activity | 100 | 356 | 688 | 782 |
| 100 | errors | 0 | 0 | 0 | 0 |

Server-side connector latency (includes simulated system latency, retries):

| Metric | n | p50 | p95 | p99 |
|---|---|---|---|---|
| connector.cards | 160 | 160.9 | 464.4 | 604.9 |
| connector.crm | 160 | 73.2 | 194.0 | 225.8 |
| connector.loans | 320 | 116.9 | 331.0 | 481.0 |
| connector.ops | 160 | 277.7 | 728.9 | 901.3 |
| connector.policy | 1 | 34.4 | 34.4 | 34.4 |
| tool.get_card_summary | 160 | 161.0 | 464.5 | 605.0 |
| tool.get_loan_summary | 160 | 259.1 | 534.8 | 712.7 |
| tool.list_service_requests | 160 | 277.8 | 729.0 | 901.5 |
