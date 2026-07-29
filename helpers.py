from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from extensions import db
from models import ExpenseSplit

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def parse_money(value, field_name, maximum):
    try:
        amount = Decimal(str(value).strip()).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {field_name}.")
    if amount <= ZERO:
        raise ValueError(f"{field_name.capitalize()} must be greater than zero.")
    if amount > Decimal(str(maximum)):
        raise ValueError(f"{field_name.capitalize()} cannot exceed ₹{maximum:,.2f}.")
    return amount


def compute_balances(group):
    """Return each member's net balance; positive means they should receive."""
    balances = defaultdict(lambda: ZERO)
    for expense in group.expenses:
        for split in expense.splits:
            if split.member_id != expense.paid_by_id:
                balances[split.member_id] -= split.amount_owed
                balances[expense.paid_by_id] += split.amount_owed
    return balances


def compute_settlements(balances):
    """Greedily reduce balances to a small list of payments."""
    creditors = [[member_id, amount] for member_id, amount in balances.items() if amount > ZERO]
    debtors = [[member_id, -amount] for member_id, amount in balances.items() if amount < ZERO]
    creditors.sort(key=lambda item: item[1], reverse=True)
    debtors.sort(key=lambda item: item[1], reverse=True)

    settlements, debtor_index, creditor_index = [], 0, 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor_id, debt = debtors[debtor_index]
        creditor_id, credit = creditors[creditor_index]
        payment = min(debt, credit)
        settlements.append((debtor_id, creditor_id, payment))
        debtors[debtor_index][1] -= payment
        creditors[creditor_index][1] -= payment
        if debtors[debtor_index][1] == ZERO:
            debtor_index += 1
        if creditors[creditor_index][1] == ZERO:
            creditor_index += 1
    return settlements


def save_splits(expense, members, expense_type, form, maximum):
    """Validate and create split rows for an expense."""
    if expense_type == "equal":
        amount = parse_money(form.get("amount"), "amount", maximum)
        shared_ids = {int(member_id) for member_id in form.getlist("shared_by")}
        valid_ids = {member.id for member in members}
        if not shared_ids or not shared_ids.issubset(valid_ids):
            raise ValueError("Select valid group members to share the expense.")

        # Keep the stored total exact while assigning leftover cents to early shares.
        share = (amount / Decimal(len(shared_ids))).quantize(CENT, rounding=ROUND_HALF_UP)
        if share * len(shared_ids) > amount:
            share -= CENT
        remainder_cents = int((amount - (share * len(shared_ids))) / CENT)
        for index, member_id in enumerate(sorted(shared_ids)):
            owed = share + (CENT if index < remainder_cents else ZERO)
            db.session.add(ExpenseSplit(expense_id=expense.id, member_id=member_id, amount_owed=owed))
        expense.amount = amount
        return

    if expense_type != "custom":
        raise ValueError("Choose either an equal or custom split.")

    expected_total = parse_money(form.get("custom_total"), "total amount", maximum)
    split_total = ZERO
    for member in members:
        raw_amount = form.get(f"custom_{member.id}", "").strip()
        if not raw_amount:
            continue
        amount = parse_money(raw_amount, f"amount for {member.name}", maximum)
        db.session.add(ExpenseSplit(expense_id=expense.id, member_id=member.id, amount_owed=amount))
        split_total += amount

    if split_total != expected_total:
        raise ValueError(f"Custom split total (₹{split_total:.2f}) must equal the entered amount (₹{expected_total:.2f}).")
    expense.amount = expected_total
