from decimal import Decimal

from extensions import db
from sqlalchemy import or_

from models import ActivityEvent, ConfirmedSettlement, SettlementRequest


def _money_text(value):
    return format(Decimal(str(value)).quantize(Decimal("0.01")), ".2f")


def record_expense_created(record, expense, actor_user_id):
    """Add structured audit metadata in the caller's accounting transaction."""
    db.session.add(ActivityEvent(
        actor_user_id=actor_user_id,
        event_type="expense_created",
        group_id=record.group_id,
        expense_record_id=record.id,
        details={
            "description": expense.description or "",
            "amount": _money_text(expense.amount),
        },
    ))


def record_expense_revised(record, before, after, actor_user_id):
    """Record only compact display facts; revisions remain the audit source."""
    db.session.add(ActivityEvent(
        actor_user_id=actor_user_id,
        event_type="expense_revised",
        group_id=record.group_id,
        expense_record_id=record.id,
        details={
            "description": after.description or before.description or "",
            "before_amount": _money_text(before.amount),
            "after_amount": _money_text(after.amount),
        },
    ))


def settlement_history_entries(user_id):
    """Present payment audit sources in the existing history UI, privately.

    Expense events retain their group visibility; a global payment is visible
    only to its two participants. No synthetic group or expense is invented.
    """
    payments = ConfirmedSettlement.query.filter(or_(
        ConfirmedSettlement.payer_user_id == user_id, ConfirmedSettlement.recipient_user_id == user_id)).all()
    entries = []
    for payment in payments:
        entries.append({"kind": "payment", "created_at": payment.confirmed_at, "payment": payment})
        if payment.reversal is not None:
            entries.append({"kind": "reversal", "created_at": payment.reversal.reversed_at, "payment": payment})
    # Include unmatched historical requests without claiming they were payments.
    requests = SettlementRequest.query.filter(or_(
        SettlementRequest.requester_user_id == user_id, SettlementRequest.recipient_user_id == user_id
    )).filter(~SettlementRequest.id.in_(db.session.query(ConfirmedSettlement.settlement_request_id))).all()
    for historical in requests:
        entries.append({"kind": "legacy_request", "created_at": historical.created_at, "request": historical})
    entries.sort(key=lambda entry: entry["created_at"], reverse=True)
    return entries
