"""Check data/customers.yaml before restarting or deploying.

    python scripts/validate_customers.py [path]

Prints every problem by customer and field; exit code 1 if any.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from ledgerflow.mocks.personas import PersonaError, demo_customers, load_personas  # noqa: E402
from ledgerflow.mocks.seed import CUSTOMERS_FILE  # noqa: E402

path = Path(sys.argv[1]) if len(sys.argv) > 1 else CUSTOMERS_FILE
try:
    people = load_personas(path)
except PersonaError as e:
    print(f"FAIL  {e}")
    sys.exit(1)
print(f"OK    {path}: {len(people)} customers, {len(demo_customers(people))} on the sign-in screen")
for c in people:
    parts = [p for p in ("loan" if c.get("loan") else "", "card" if c.get("card") else "") if p]
    print(f"      {c['id']}  {c['name']:<18} {c['phone']}  {' + '.join(parts)}{'  [demo]' if c.get('demo') else ''}")
