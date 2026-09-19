from decimal import Decimal

from extensions import db
from models import ActivityEvent


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
