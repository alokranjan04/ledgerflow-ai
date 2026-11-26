"""Pure loan arithmetic. Decimal everywhere, half-up rounding to the paisa.

Every number the assistant states about a loan comes from here (via the
domain tools), never from the language model.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

PAISA = Decimal("0.01")
GST_RATE = Decimal("0.18")


def money(x) -> Decimal:
    return Decimal(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def add_months(d: date, months: int, day: int | None = None) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day or d.day, last))


def emi(principal: Decimal, annual_rate_pct: Decimal, months: int) -> Decimal:
    """Equated monthly instalment, rounded half-up to the paisa."""
    principal = Decimal(principal)
    if months <= 0:
        raise ValueError("months must be positive")
    r = Decimal(annual_rate_pct) / Decimal(1200)
    if r == 0:
        return money(principal / months)
    f = (1 + r) ** months
    return money(principal * r * f / (f - 1))


@dataclass
class Instalment:
    no: int
    due_date: date
    rate_pct: Decimal
    emi: Decimal
    interest: Decimal
    principal: Decimal
    opening_balance: Decimal
    closing_balance: Decimal

    def as_dict(self) -> dict:
        return {"no": self.no, "due_date": self.due_date.isoformat(), "rate_pct": str(self.rate_pct),
                "emi": str(self.emi), "interest": str(self.interest), "principal": str(self.principal),
                "opening_balance": str(self.opening_balance), "closing_balance": str(self.closing_balance)}


@dataclass
class RateChange:
    effective: date
    rate_pct: Decimal
    reason: str = ""


@dataclass
class Schedule:
    rows: list[Instalment] = field(default_factory=list)

    def paid_until(self, as_of: date) -> list[Instalment]:
        return [r for r in self.rows if r.due_date <= as_of]

    def upcoming(self, as_of: date) -> list[Instalment]:
        return [r for r in self.rows if r.due_date > as_of]


def rate_on(history: list[RateChange], d: date) -> Decimal:
    applicable = [h for h in sorted(history, key=lambda h: h.effective) if h.effective <= d]
    if not applicable:
        return sorted(history, key=lambda h: h.effective)[0].rate_pct
    return applicable[-1].rate_pct


def build_schedule(principal: Decimal, tenure_months: int, first_due: date,
                   history: list[RateChange]) -> Schedule:
    """Amortisation schedule. On a rate reset the EMI is recomputed over the
    remaining tenure (the tenure stays fixed), which is how a floating-rate EMI
    "goes up". The rate for an instalment is the rate in force on the day the
    period starts (the previous due date)."""
    balance = money(principal)
    rows: list[Instalment] = []
    current_rate: Decimal | None = None
    current_emi = Decimal(0)
    for i in range(1, tenure_months + 1):
        due = add_months(first_due, i - 1)
        period_start = add_months(first_due, i - 2)
        rate = rate_on(history, period_start)
        remaining = tenure_months - i + 1
        if rate != current_rate:
            current_rate = rate
            current_emi = emi(balance, rate, remaining)
        interest = money(balance * rate / Decimal(1200))
        if i == tenure_months or current_emi - interest >= balance:
            principal_part = balance
            pay = money(principal_part + interest)
        else:
            principal_part = money(current_emi - interest)
            pay = current_emi
        closing = money(balance - principal_part)
        rows.append(Instalment(i, due, rate, pay, interest, principal_part, balance, closing))
        balance = closing
        if balance <= 0:
            break
    return Schedule(rows)


def amortise(balance: Decimal, rate_pct: Decimal, monthly: Decimal, max_months: int = 600) -> tuple[int, Decimal]:
    """Pay a fixed instalment until the balance is gone. Returns (months, total interest)."""
    balance = money(balance)
    months, total_interest = 0, Decimal(0)
    r = Decimal(rate_pct) / Decimal(1200)
    while balance > 0 and months < max_months:
        interest = money(balance * r)
        if monthly <= interest:
            raise ValueError("instalment does not cover interest")
        principal_part = min(balance, monthly - interest)
        # Lenders fold a rounding residue into the final EMI instead of
        # billing an extra month for a few rupees.
        if balance - principal_part < monthly * Decimal("0.02"):
            principal_part = balance
        balance = money(balance - principal_part)
        total_interest += interest
        months += 1
    return months, money(total_interest)


@dataclass
class PrepaymentOutcome:
    amount: Decimal
    baseline_months: int
    baseline_interest: Decimal
    current_emi: Decimal
    reduce_tenure_months: int
    reduce_tenure_interest: Decimal
    reduce_tenure_saving: Decimal
    reduce_emi_new_emi: Decimal
    reduce_emi_interest: Decimal
    reduce_emi_saving: Decimal

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def simulate_prepayment(outstanding: Decimal, rate_pct: Decimal, current_emi: Decimal,
                        remaining_months: int, amount: Decimal) -> PrepaymentOutcome:
    """Compare keeping the EMI (shorter tenure) with keeping the tenure (lower EMI),
    assuming the current rate stays unchanged for the rest of the loan."""
    amount = money(amount)
    if amount <= 0 or amount >= outstanding:
        raise ValueError("prepayment must be positive and less than the outstanding principal")
    base_months, base_interest = amortise(outstanding, rate_pct, current_emi)
    new_balance = money(outstanding - amount)
    rt_months, rt_interest = amortise(new_balance, rate_pct, current_emi)
    new_emi = emi(new_balance, rate_pct, remaining_months)
    re_months, re_interest = amortise(new_balance, rate_pct, new_emi)
    return PrepaymentOutcome(
        amount=amount, baseline_months=base_months, baseline_interest=base_interest,
        current_emi=current_emi,
        reduce_tenure_months=rt_months, reduce_tenure_interest=rt_interest,
        reduce_tenure_saving=money(base_interest - rt_interest),
        reduce_emi_new_emi=new_emi, reduce_emi_interest=re_interest,
        reduce_emi_saving=money(base_interest - re_interest),
    )


def accrued_interest(balance: Decimal, rate_pct: Decimal, since: date, until: date) -> Decimal:
    """Simple daily interest, actual/365, for the broken period before foreclosure."""
    days = max(0, (until - since).days)
    return money(Decimal(balance) * Decimal(rate_pct) / Decimal(100) * days / Decimal(365))


def charge_with_gst(base: Decimal, pct: Decimal) -> tuple[Decimal, Decimal]:
    charge = money(Decimal(base) * Decimal(pct) / Decimal(100))
    return charge, money(charge * GST_RATE)


def inr(x, decimals: bool | None = None) -> str:
    """Indian digit grouping: 1234567.5 -> '₹12,34,567.50'. Whole rupees drop the paise."""
    d = money(x)
    neg = d < 0
    d = abs(d)
    whole, frac = f"{d:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    show_paise = decimals if decimals is not None else frac != "00"
    return f"{'-' if neg else ''}₹{whole}{'.' + frac if show_paise else ''}"
