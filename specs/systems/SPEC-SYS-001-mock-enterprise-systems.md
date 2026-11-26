# SPEC-SYS-001: Mock enterprise systems
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenge 4: build or simulate the CRM, Loan Management, Payments, Policy Knowledge and Service
Operations systems, which "have different response times and may occasionally fail". Code:
`ledgerflow/mocks/server.py`.

## Requirements
- **REQ-SYS-001.1** [MUST] THE SYSTEM SHALL expose CRM, Loans, Payments, Policy, Service Ops and Cards as HTTP APIs under `/mock/<system>/`, called by the assistant only through connectors.
- **REQ-SYS-001.2** [MUST] THE SYSTEM SHALL delay each response using a per-system log-normal latency profile defined by p50 and p95.
- **REQ-SYS-001.3** [MUST] WHEN an operator changes a system's profile (error rate, slow, down) at runtime THE SYSTEM SHALL apply it to the next requests without a restart.
- **REQ-SYS-001.4** [MUST] Service Ops write endpoints SHALL require an `Idempotency-Key`, and a replayed key SHALL return the original request without creating a duplicate.
- **REQ-SYS-001.5** [SHOULD] Service Ops SHALL support escalation of an open request (open → escalated).

## Interfaces
`GET /mock/crm/customers/{id}`, `POST /mock/crm/verify`, `GET /mock/loans/customers/{id}/loans`,
`GET /mock/loans/loans/{id}` (terms, rate history, schedule), `GET /mock/payments/loans/{id}/payments?since=`,
`GET /mock/policy/products/{code}`, `GET /mock/policy/search?q=`, `POST|GET /mock/ops/requests`,
`POST /mock/ops/requests/{id}/escalate`, `GET /mock/cards/customers/{id}/cards`,
`GET /mock/cards/cards/{id}/transactions`, `GET|POST /mock/admin/faults` (also `/api/faults`).

## Acceptance criteria
- Given loans at p50 120 ms / p95 350 ms, 4,000 samples land within ±10% (p50) and ±15% (p95). → .2
- Given payments set to down, a servicing-options question still answers and names Payments as unavailable. → .3
- Given a request posted twice with the same key, the second returns the same ID with `replayed: true`. → .4

## Non-goals
Real authentication between services; separate deployment per system (a deployment change only).
