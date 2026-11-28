# Latency — conversations — 2026-11-28 08:48 — target http://localhost:8092

| Concurrent conversations | Step | n | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| 250 | login | 250 | 299 | 484 | 617 |
| 250 | connect | 250 | 3 | 7 | 8 |
| 250 | turn | 750 | 1158 | 2547 | 3323 |
| 250 | errors | 0 | 0 | 0 | 0 |
| 500 | login | 500 | 621 | 987 | 1051 |
| 500 | connect | 500 | 3 | 8 | 9 |
| 500 | turn | 1500 | 2194 | 4770 | 6021 |
| 500 | errors | 1 | 0 | 0 | 0 |
| 1000 | login | 1000 | 1253 | 2051 | 2132 |
| 1000 | connect | 1000 | 4 | 9 | 13 |
| 1000 | turn | 3000 | 4331 | 8012 | 8040 |
| 1000 | errors | 227 | 0 | 0 | 0 |

Each conversation: sign in, open the chat WebSocket, wait until ALL are open, then ask three questions in turn (fast lane, no language model). `turn` is question sent → answer and trace received.

Server-side (includes simulated system latency, bulkheads, retries):

| Metric | n | p50 | p95 | p99 |
|---|---|---|---|---|
| connector.crm | 1750 | 593.1 | 1543.9 | 1642.3 |
| connector.loans | 2000 | 866.5 | 2328.6 | 2885.3 |
| connector.payments | 1522 | 1801.9 | 4523.1 | 4909.6 |
| tool.check_foreclosure | 1750 | 1314.8 | 3492.4 | 3870.2 |
| tool.get_loan_summary | 1750 | 3559.4 | 5932.8 | 6222.3 |
| tool.get_payments | 1750 | 4564.8 | 7999.5 | 8011.9 |
| turn.text.fast_lane | 2000 | 3654.6 | 8013.7 | 8026.5 |
| turn.text.total | 2000 | 3654.6 | 8013.7 | 8026.5 |
