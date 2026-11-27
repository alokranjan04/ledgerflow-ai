"""Check data/policies.yaml and data/customers.yaml before restarting or deploying.

    python scripts/validate_customers.py [customers.yaml] [--policies policies.yaml]

Prints every problem by product / customer and field; exit code 1 if any.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from ledgerflow.mocks.personas import PersonaError, demo_customers, load_personas  # noqa: E402
from ledgerflow.mocks.policies import POLICIES_FILE, PolicyError, load_policies  # noqa: E402
from ledgerflow.mocks.seed import CUSTOMERS_FILE  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("customers", nargs="?", default=str(CUSTOMERS_FILE))
ap.add_argument("--policies", default=str(POLICIES_FILE))
a = ap.parse_args()

try:
    products = load_policies(a.policies)
except PolicyError as e:
    print(f"FAIL  {e}")
    sys.exit(1)
print(f"OK    {a.policies}: {len(products)} products")
for code, p in products.items():
    pol = p["policy"]
    print(f"      {code:<10} {p['kind']:<5} {p['rate_type']:<9} v{pol['version']} from {pol['effective']}  "
          f"{len(pol['rules'])} rules, {len(pol['clauses'])} clauses  {p['name']}")

try:
    people = load_personas(Path(a.customers), products)
except PersonaError as e:
    print(f"FAIL  {e}")
    sys.exit(1)
print(f"OK    {a.customers}: {len(people)} customers, {len(demo_customers(people))} on the sign-in screen")
for c in people:
    parts = [f"loan {c['loan']['product']}" if c.get("loan") else "",
             f"card {c['card'].get('product', 'default')}" if c.get("card") else ""]
    print(f"      {c['id']}  {c['name']:<18} {c['phone']}  {' + '.join(p for p in parts if p)}"
          f"{'  [demo]' if c.get('demo') else ''}")
