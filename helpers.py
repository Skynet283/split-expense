from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from extensions import db
from expense_history import iter_active_expenses
from models import ConfirmedSettlement, ExpenseSplit, SettlementRequest

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def create_expense_split(expense_id, member_id, amount_owed):
    """Build a split without relying on SQLAlchemy's dynamic constructor."""
    split = ExpenseSplit()
    split.expense_id = expense_id
    split.member_id = member_id
    split.amount_owed = amount_owed
    return split


def create_user_expense_split(expense_id, member_id, user_id, amount_owed):
    """Create a transition split with both legacy and global identities."""
    split = create_expense_split(expense_id, member_id, amount_owed)
    split.user_id = user_id
    return split


def parse_money(value, field_name, maximum):
    try:
        amount = Decimal(str(value).strip()).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {field_name}.")
    if not amount.is_finite():
        raise ValueError(f"Enter a valid {field_name}.")
    if amount <= ZERO:
        raise ValueError(f"{field_name.capitalize()} must be greater than zero.")
    if amount > Decimal(str(maximum)):
        raise ValueError(f"{field_name.capitalize()} cannot exceed ₹{maximum:,.2f}.")
    return amount


def compute_balances(group):
    """Return each member's net balance; positive means they should receive."""
    balances = defaultdict(lambda: ZERO)
    for expense in iter_active_expenses(group):
        for split in expense.splits:
            if split.member_id != expense.paid_by_id:
                balances[split.member_id] -= split.amount_owed
                balances[expense.paid_by_id] += split.amount_owed
    return balances


def compute_user_balances(group):
    """Return group balances for expense entries that have global User links.

    Legacy rows without those links are intentionally excluded here rather than
    guessed from a display name.  Callers can retain ``compute_balances`` for
    the complete legacy-compatible view.
    """
    balances = defaultdict(lambda: ZERO)
    for expense in iter_active_expenses(group):
        if expense.paid_by_user_id is None:
            continue
        for split in expense.splits:
            if split.user_id is None:
                continue
            if split.user_id != expense.paid_by_user_id:
                balances[split.user_id] -= split.amount_owed
                balances[expense.paid_by_user_id] += split.amount_owed
    return balances


def compute_global_user_balances():
    """Aggregate fully mapped expenses and independent confirmed payments.

    Returns ``(balances, unresolved)``.  ``unresolved`` contains structured
    records for expenses excluded because their payer or any split lacks a
    global User link (or their stored splits do not account for the amount).
    Such records are never partially applied or identity-mapped by name.
    """
    balances = defaultdict(lambda: ZERO)
    unresolved = []
    for expense in iter_active_expenses():
        splits = expense.splits
        if expense.paid_by_user_id is None:
            unresolved.append({"expense_id": expense.id, "reason": "payer_unmapped"})
            continue
        if not splits:
            unresolved.append({"expense_id": expense.id, "reason": "splits_missing"})
            continue
        if any(split.user_id is None for split in splits):
            unresolved.append({"expense_id": expense.id, "reason": "split_unmapped"})
            continue

        amount = Decimal(str(expense.amount)).quantize(CENT, rounding=ROUND_HALF_UP)
        split_total = sum(
            (Decimal(str(split.amount_owed)).quantize(CENT, rounding=ROUND_HALF_UP) for split in splits),
            ZERO,
        )
        if split_total != amount:
            unresolved.append({"expense_id": expense.id, "reason": "split_total_mismatch"})
            continue

        balances[expense.paid_by_user_id] += amount
        for split in splits:
            balances[split.user_id] -= Decimal(str(split.amount_owed)).quantize(CENT, rounding=ROUND_HALF_UP)
    for payment in ConfirmedSettlement.query.all():
        amount = Decimal(str(payment.amount)).quantize(CENT)
        balances[payment.payer_user_id] += amount
        balances[payment.recipient_user_id] -= amount
    return balances, unresolved


def compute_pairwise_user_obligations():
    """Return net direct debtor-to-creditor obligations from mapped expenses.

    Only complete global-user expenses are considered.  Reciprocal obligations
    are netted per pair, but no obligation is inferred from a user's name,
    legacy Member ID, or global balance alone.
    """
    raw_obligations = defaultdict(lambda: ZERO)
    unresolved = []
    for expense in iter_active_expenses():
        splits = expense.splits
        if expense.paid_by_user_id is None:
            unresolved.append({"expense_id": expense.id, "reason": "payer_unmapped"})
            continue
        if not splits:
            unresolved.append({"expense_id": expense.id, "reason": "splits_missing"})
            continue
        if any(split.user_id is None for split in splits):
            unresolved.append({"expense_id": expense.id, "reason": "split_unmapped"})
            continue
        amount = Decimal(str(expense.amount)).quantize(CENT, rounding=ROUND_HALF_UP)
        split_total = sum((Decimal(str(split.amount_owed)).quantize(CENT, rounding=ROUND_HALF_UP) for split in splits), ZERO)
        if split_total != amount:
            unresolved.append({"expense_id": expense.id, "reason": "split_total_mismatch"})
            continue
        for split in splits:
            if split.user_id != expense.paid_by_user_id:
                raw_obligations[(split.user_id, expense.paid_by_user_id)] += Decimal(str(split.amount_owed)).quantize(CENT, rounding=ROUND_HALF_UP)

    # Subtract before reciprocal netting. Negative amounts represent a credit,
    # including when the original expense has subsequently changed/disappeared.
    for payment in ConfirmedSettlement.query.all():
        raw_obligations[(payment.payer_user_id, payment.recipient_user_id)] -= Decimal(str(payment.amount)).quantize(CENT)

    obligations = {}
    processed_pairs = set()
    for debtor_id, creditor_id in list(raw_obligations):
        pair = tuple(sorted((debtor_id, creditor_id)))
        if pair in processed_pairs:
            continue
        processed_pairs.add(pair)
        first, second = pair
        # ``defaultdict[key]`` would create a missing reverse-direction key
        # while iterating.  Use non-mutating lookups for reciprocal netting.
        first_owes_second = raw_obligations.get((first, second), ZERO)
        second_owes_first = raw_obligations.get((second, first), ZERO)
        if first_owes_second > second_owes_first:
            obligations[(first, second)] = first_owes_second - second_owes_first
        elif second_owes_first > first_owes_second:
            obligations[(second, first)] = second_owes_first - first_owes_second
    return obligations, unresolved


def parse_settlement_amount(value):
    """Parse a positive, exact-cent settlement amount without float rounding."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid settlement amount.")
    if not amount.is_finite() or amount <= ZERO:
        raise ValueError("Settlement amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Settlement amount may have at most two decimal places.")
    if amount > Decimal("9999999999.99"):
        raise ValueError("Settlement amount exceeds the supported maximum.")
    return amount.quantize(CENT)


def pending_settlement_amount(requester_user_id, recipient_user_id):
    pending = SettlementRequest.query.filter_by(
        requester_user_id=requester_user_id, recipient_user_id=recipient_user_id, status="PENDING"
    ).all()
    return sum((Decimal(str(request.amount)).quantize(CENT) for request in pending), ZERO)


def get_user_settlement_summary(user_id):
    """Prepare direct-obligation settlement rows for one dashboard user.

    Confirmed payments are already represented by
    ``compute_pairwise_user_obligations``. This view model reserves only
    pending outgoing requests, so it never changes ledger accounting.
    """
    obligations, _ = compute_pairwise_user_obligations()
    pending_by_pair = defaultdict(lambda: ZERO)
    pending_requests = SettlementRequest.query.filter_by(status="PENDING").filter(
        (SettlementRequest.requester_user_id == user_id)
        | (SettlementRequest.recipient_user_id == user_id)
    )
    for request in pending_requests:
        pair = (request.requester_user_id, request.recipient_user_id)
        pending_by_pair[pair] += Decimal(str(request.amount)).quantize(CENT)

    outgoing, incoming = [], []
    for (debtor_id, creditor_id), outstanding in obligations.items():
        if debtor_id == user_id:
            pending = pending_by_pair[(debtor_id, creditor_id)]
            outgoing.append({
                "user_id": creditor_id,
                "amount": outstanding,
                "outstanding_amount": outstanding,
                "pending_amount": pending,
                "available_amount": max(ZERO, outstanding - pending),
            })
        elif creditor_id == user_id:
            incoming.append({
                "user_id": debtor_id,
                "amount": outstanding,
                "outstanding_amount": outstanding,
                "pending_amount": pending_by_pair[(debtor_id, creditor_id)],
            })
    return outgoing, incoming


def validate_settlement_request(requester_user_id, recipient_user_id, amount):
    """Validate a proposed request against direct mapped financial obligations."""
    if requester_user_id == recipient_user_id:
        raise ValueError("You cannot create a settlement request to yourself.")
    obligations, _ = compute_pairwise_user_obligations()
    outstanding = obligations.get((requester_user_id, recipient_user_id), ZERO)
    if outstanding <= ZERO:
        raise ValueError("No valid outstanding obligation exists for this settlement request.")
    if pending_settlement_amount(requester_user_id, recipient_user_id) + amount > outstanding:
        raise ValueError("Pending settlement requests exceed the valid outstanding obligation.")
    return outstanding


def validate_settlement_confirmation(request):
    """Reject stale pending requests without changing their workflow state."""
    obligations, _ = compute_pairwise_user_obligations()
    outstanding = obligations.get((request.requester_user_id, request.recipient_user_id), ZERO)
    if parse_settlement_amount(request.amount) > outstanding:
        raise ValueError("This settlement request is stale and exceeds the current outstanding obligation.")


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


def parse_share_value(value, field_name):
    """Accept a bounded, exact, nonnegative decimal weight (zero is valid)."""
    raw = str(value).strip()
    if len(raw) > 100:
        raise ValueError(f"Share value for {field_name} is outside the supported range.")
    try:
        share = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid share value for {field_name}.") from None
    if not share.is_finite() or share < 0:
        raise ValueError(f"Enter a nonnegative, finite share value for {field_name}.")
    if share == 0:
        return Decimal(0)
    # Bound work and storage for hostile exponents/huge coefficients. This is
    # far beyond normal share inputs while keeping integer allocation finite.
    if len(share.as_tuple().digits) > 36 or abs(share.as_tuple().exponent) > 36:
        raise ValueError(f"Share value for {field_name} is outside the supported range.")
    return share


def parse_share_inputs(form, participants):
    """Blank fields omit a person; an explicit zero retains that participant."""
    shares = {}
    for participant in participants:
        values = form.getlist(f"share_{participant.id}")
        if len(values) > 1:
            raise ValueError("Enter one share value per participant.")
        if not values or not values[0].strip():
            continue
        name = getattr(participant, "name", None) or getattr(participant, "login_id", str(participant.id))
        shares[participant.id] = parse_share_value(values[0], name)
    if not any(share > 0 for share in shares.values()):
        raise ValueError("At least one participant must have more than zero shares.")
    return shares


def allocate_share_amounts(amount, shares):
    """Allocate exact cents by largest fractional remainder, then stable ID."""
    scale = max(0, -min(share.as_tuple().exponent for share in shares.values() if share))
    integer_weights = {}
    for participant_id, share in shares.items():
        if not share:
            integer_weights[participant_id] = 0
            continue
        decimal_tuple = share.as_tuple()
        coefficient = int("".join(str(digit) for digit in decimal_tuple.digits))
        integer_weights[participant_id] = coefficient * 10 ** (decimal_tuple.exponent + scale)
    total_weight = sum(integer_weights.values())
    if total_weight <= 0:
        raise ValueError("At least one participant must have more than zero shares.")
    total_cents = int(amount / CENT)
    cents, remainders = {}, {}
    for participant_id, weight in integer_weights.items():
        cents[participant_id], remainders[participant_id] = divmod(total_cents * weight, total_weight)
    leftover = total_cents - sum(cents.values())
    for participant_id in sorted(shares, key=lambda key: (-remainders[key], key))[:leftover]:
        cents[participant_id] += 1
    return {participant_id: Decimal(cents[participant_id]) * CENT for participant_id in sorted(shares)}


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
            db.session.add(create_expense_split(expense.id, member_id, owed))
        expense.amount = amount
        return

    if expense_type == "shares":
        amount = parse_money(form.get("share_total"), "total amount", maximum)
        shares = parse_share_inputs(form, members)
        allocations = allocate_share_amounts(amount, shares)
        for member_id, owed in allocations.items():
            split = create_expense_split(expense.id, member_id, owed)
            split.share_weight = str(shares[member_id])
            db.session.add(split)
        expense.amount = amount
        return

    if expense_type != "custom":
        raise ValueError("Choose an equal, custom, or shares split.")

    expected_total = parse_money(form.get("custom_total"), "total amount", maximum)
    split_total = ZERO
    for member in members:
        raw_amount = form.get(f"custom_{member.id}", "").strip()
        if not raw_amount:
            continue
        amount = parse_money(raw_amount, f"amount for {member.name}", maximum)
        db.session.add(create_expense_split(expense.id, member.id, amount))
        split_total += amount

    if split_total != expected_total:
        raise ValueError(f"Custom split total (₹{split_total:.2f}) must equal the entered amount (₹{expected_total:.2f}).")
    expense.amount = expected_total


def save_user_splits(expense, users, legacy_members_by_user, expense_type, form, maximum):
    """Validate and save a new global-user expense while retaining legacy IDs."""
    users_by_id = {user.id: user for user in users}
    if expense_type == "equal":
        amount = parse_money(form.get("amount"), "amount", maximum)
        shared_ids = {int(user_id) for user_id in form.getlist("shared_by")}
        if not shared_ids or not shared_ids.issubset(users_by_id):
            raise ValueError("Select valid group users to share the expense.")
        share = (amount / Decimal(len(shared_ids))).quantize(CENT, rounding=ROUND_HALF_UP)
        if share * len(shared_ids) > amount:
            share -= CENT
        remainder_cents = int((amount - (share * len(shared_ids))) / CENT)
        for index, user_id in enumerate(sorted(shared_ids)):
            owed = share + (CENT if index < remainder_cents else ZERO)
            db.session.add(create_user_expense_split(expense.id, legacy_members_by_user[user_id].id, user_id, owed))
        expense.amount = amount
        return

    if expense_type == "shares":
        amount = parse_money(form.get("share_total"), "total amount", maximum)
        shares = parse_share_inputs(form, users)
        allocations = allocate_share_amounts(amount, shares)
        for user_id, owed in allocations.items():
            split = create_user_expense_split(expense.id, legacy_members_by_user[user_id].id, user_id, owed)
            split.share_weight = str(shares[user_id])
            db.session.add(split)
        expense.amount = amount
        return

    if expense_type != "custom":
        raise ValueError("Choose an equal, custom, or shares split.")
    expected_total = parse_money(form.get("custom_total"), "total amount", maximum)
    split_total = ZERO
    for user in users:
        raw_amount = form.get(f"custom_{user.id}", "").strip()
        if not raw_amount:
            continue
        amount = parse_money(raw_amount, f"amount for {user.name or user.login_id}", maximum)
        db.session.add(create_user_expense_split(expense.id, legacy_members_by_user[user.id].id, user.id, amount))
        split_total += amount
    if split_total != expected_total:
        raise ValueError(f"Custom split total (₹{split_total:.2f}) must equal the entered amount (₹{expected_total:.2f}).")
    expense.amount = expected_total
