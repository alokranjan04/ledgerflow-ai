# Latency — conversations — 2026-11-28 08:47 — target http://localhost:8092

| Concurrent conversations | Step | n | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| 100 | login | 100 | 180 | 286 | 436 |
| 100 | connect | 100 | 2 | 5 | 6 |
| 100 | turn | 300 | 516 | 1112 | 1576 |
| 100 | errors | 0 | 0 | 0 | 0 |
| 250 | login | 250 | 271 | 498 | 563 |
| 250 | connect | 250 | 3 | 5 | 6 |
| 250 | turn | 750 | 1118 | 2236 | 2914 |
| 250 | errors | 0 | 0 | 0 | 0 |

Each conversation: sign in, open the chat WebSocket, wait until ALL are open, then ask three questions in turn (fast lane, no language model). `turn` is question sent → answer and trace received.

Server-side (includes simulated system latency, bulkheads, retries):

| Metric | n | p50 | p95 | p99 |
|---|---|---|---|---|
| connector.crm | 350 | 176.0 | 395.8 | 487.9 |
| connector.loans | 1750 | 300.6 | 848.5 | 1028.2 |
| connector.payments | 350 | 721.4 | 1687.8 | 2394.0 |
| tool.check_foreclosure | 350 | 345.5 | 810.3 | 1077.4 |
| tool.get_loan_summary | 350 | 1022.2 | 1527.1 | 1865.3 |
| tool.get_payments | 350 | 1425.7 | 2481.6 | 3192.5 |
| turn.text.fast_lane | 1050 | 852.8 | 2156.7 | 2830.9 |
| turn.text.total | 1050 | 852.8 | 2156.7 | 2830.9 |
