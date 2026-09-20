from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func

from extensions import db
from models import Expense, ExpenseRecord, ExpenseRevision


def active_expense_records(group=None):
    """Return logical records with their one current immutable snapshot."""
    query = ExpenseRecord.query.filter(ExpenseRecord.current_expense_id.isnot(None))
    if group is not None:
        query = query.filter_by(group_id=group.id)
    return query.order_by(ExpenseRecord.id).all()


def iter_active_expenses(group=None):
    """Canonical source for every current-accounting and display path."""
    for record in active_expense_records(group):
        yield record.current_expense


def create_expense_record(expense, actor_user_id):
    """Finalize a newly built Expense snapshot as revision one."""
    db.session.flush()
    record = ExpenseRecord(group_id=expense.group_id, current_expense_id=expense.id,
                           created_by_user_id=actor_user_id)
    db.session.add(record)
    db.session.flush()
    db.session.add(ExpenseRevision(expense_record_id=record.id, expense_id=expense.id,
                                   version_number=1, recorded_by_user_id=actor_user_id))
    return record


def claim_active_expense_record(record_id):
    """Claim and reload one active root inside the caller's transaction."""
    claimed = ExpenseRecord.query.filter_by(id=record_id, voided_at=None).filter(
        ExpenseRecord.current_expense_id.isnot(None)
    ).update({ExpenseRecord.current_expense_id: ExpenseRecord.current_expense_id}, synchronize_session=False)
    if claimed != 1:
        raise ValueError("Only an active expense can be changed.")
    record = db.session.get(ExpenseRecord, record_id)
    db.session.refresh(record)
    return record


def _money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def expense_snapshots_equal(first, second):
    first_splits = sorted((split.member_id, split.user_id, _money(split.amount_owed),
                           split.share_weight is not None,
                           Decimal(split.share_weight) if split.share_weight is not None else Decimal(0))
                          for split in first.splits)
    second_splits = sorted((split.member_id, split.user_id, _money(split.amount_owed),
                            split.share_weight is not None,
                            Decimal(split.share_weight) if split.share_weight is not None else Decimal(0))
                           for split in second.splits)
    return (
        (first.description or "") == (second.description or "")
        and first.paid_by_id == second.paid_by_id
        and first.paid_by_user_id == second.paid_by_user_id
        and _money(first.amount) == _money(second.amount)
        and first.expense_type == second.expense_type
        and first_splits == second_splits
    )


def append_expense_revision(record, expense, actor_user_id):
    """Append a snapshot and move the active pointer; return False for a no-op."""
    db.session.flush()
    if expense_snapshots_equal(record.current_expense, expense):
        db.session.delete(expense)
        db.session.flush()
        return False
    latest = db.session.query(func.max(ExpenseRevision.version_number)).filter_by(
        expense_record_id=record.id
    ).scalar() or 0
    db.session.add(ExpenseRevision(expense_record_id=record.id, expense_id=expense.id,
                                   version_number=latest + 1, recorded_by_user_id=actor_user_id))
    record.current_expense_id = expense.id
    return True


def void_expense_record(record_id, actor_user_id):
    record = claim_active_expense_record(record_id)
    record.current_expense_id = None
    record.voided_at = datetime.now(timezone.utc)
    record.voided_by_user_id = actor_user_id
    return record
