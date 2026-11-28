# SPEC-ACC-001: Customers with several loans and cards
Status: implemented     Owner: platform     Version: 1.0

## Context
A customer often holds more than one product with the same lender: a home loan and a car loan, two cards.
The first version silently used the first loan or card, so "what's my next EMI?" could answer about the wrong
account. The assistant must know every account, say which one each figure belongs to, and ask when unsure.
(Loans at *other* lenders are out of scope: a lender's assistant only sees its own books; a consent-based
aggregator would be a separate product.)

## Requirements
- **REQ-ACC-001.1** [MUST] A customer SHALL be able to hold several loans and cards (`loans:` / `cards:` lists in data/customers.yaml, or created in the admin panel), across loan products (personal, home, car) and card products; every loan and card tool, the account panel and `list_accounts` SHALL cover all of them, and every result SHALL name its account ("Car loan L-10011", "Rewards card ending 7781").
- **REQ-ACC-001.2** [MUST] Tools SHALL take an optional account (`loan_id` / `card_id`) given as an ID, the last digits, or the product word the customer used ("car", "home loan", "gaadi", "rewards"); with one account they use it; IF several fit and none was named THEN THE SYSTEM SHALL ask which one, naming each, and SHALL NOT guess or mix figures across accounts.
- **REQ-ACC-001.3** [MUST] Once the customer has named an account, follow-up questions SHALL stay on it until they name another; disputes find the account from the payment or transaction ID itself.
- **REQ-ACC-001.4** [MUST] The fast lane (no model) SHALL take the account from the question ("next EMI on my car loan"), prefix the answer with the account name when the customer holds several, and ask which one when the question does not say.
- **REQ-ACC-001.5** [MUST] The account panel SHALL list every loan and card, and reading it SHALL NOT change which account the conversation is about.
- **REQ-ACC-001.6** [MUST] Customer IDs created in the admin panel or by sign-up (C1500+, L-15000+, CARD-5500+) SHALL never collide with data/customers.yaml (C1001-C1499, checked when the file loads), and a stored admin record that clashes with a file customer SHALL leave the file's customer untouched.

## Test mapping
Generated: see `reports/traceability.md`.
