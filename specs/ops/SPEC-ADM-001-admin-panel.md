# SPEC-ADM-001: Admin panel for products, policies and customers
Status: implemented     Owner: platform     Version: 1.0

## Context
Policy and customer changes needed a file edit and a redeploy. The business (policy team, operations) needs to
change a charge, add a product, correct a customer's details or stop a compromised customer from signing in,
without a developer, with the same safety checks as the files, and with a record of who changed what.

## Requirements
- **REQ-ADM-001.1** [MUST] An admin SHALL list products with their version, source (file, file-edited, admin) and number of accounts holding them; view and edit any product's rules and clauses; create a product (blank or copied from another); revert an edited file product to its file version; and delete an admin-created product no account holds.
- **REQ-ADM-001.2** [MUST] Every product change SHALL pass the data/policies.yaml validation (reported by rule and field), SHALL need a new policy version when rules or clauses change, SHALL NOT change the kind or floating/fixed type of a product customers hold, and SHALL take effect for the assistant immediately.
- **REQ-ADM-001.3** [MUST] An admin SHALL search customers (named and registered by default, or all), view one with their accounts and open requests, edit the profile (name, mobile, date of birth, city, employment, email, sign-in-list flag and story), and create a customer with a demo loan and/or card; IDs are assigned automatically.
- **REQ-ADM-001.4** [MUST] Customer changes SHALL be validated (unique 10-digit Indian mobile, age 18-100, valid email, products that exist; a story for sign-in-list customers), and an admin SHALL be able to disable and re-enable sign-in; a disabled customer cannot sign in by app or phone and leaves the sign-in list, while accounts and requests are kept.
- **REQ-ADM-001.5** [MUST] The admin API SHALL require the admin token on a deployed service; the page holds no data without it. The token given in the URL is removed from the address bar.
- **REQ-ADM-001.6** [MUST] Admin changes SHALL survive redeploys: written to the durable store and re-applied at start-up (products first, then created customers, then profile and status changes); every change SHALL be recorded in an append-only change log with time, admin name, action, target and the exact values changed (a mobile change is recorded without the numbers).
- **REQ-ADM-001.7** [MUST] The panel SHALL work in a browser end to end (edit a policy, disable and enable a customer, read the change log) with no serious or critical accessibility violations.

## Interfaces
Page `/admin`. API (header `X-Admin-Token`; `X-Admin-User` names the admin in the change log):
`GET /api/admin/schema`, `GET /api/admin/products`, `GET|POST /api/admin/products/{code}`,
`POST /api/admin/products/{code}/revert|delete`, `GET|POST /api/admin/customers`,
`GET|POST /api/admin/customers/{id}`, `POST /api/admin/customers/{id}/status`, `GET /api/admin/audit`.
Validation failures return 422 with `errors: [..]`.

## Non-goals (production)
Named admin accounts with roles (policy editor, customer operations, read-only) via the bank's identity
provider or IAP; maker-checker approval for policy changes; editing an existing loan's financial terms.

## Test mapping
Generated: see `reports/traceability.md`.
