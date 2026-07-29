from decimal import Decimal

from extensions import db


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    members = db.relationship("Member", backref="group", lazy=True, cascade="all, delete-orphan")
    expenses = db.relationship("Expense", backref="group", lazy=True, cascade="all, delete-orphan")


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), default="")
    paid_by_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    expense_type = db.Column(db.String(20), default="equal")
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    paid_by = db.relationship("Member", foreign_keys=[paid_by_id])
    splits = db.relationship("ExpenseSplit", backref="expense", lazy=True, cascade="all, delete-orphan")


class ExpenseSplit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    amount_owed = db.Column(db.Numeric(12, 2), nullable=False)
    member = db.relationship("Member", foreign_keys=[member_id])
