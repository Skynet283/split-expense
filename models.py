from decimal import Decimal

from flask_login import UserMixin

from extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint(
            "length(login_id) = 10 AND login_id GLOB '[0-9]*' AND login_id NOT GLOB '*[^0-9]*'",
            name="ck_users_login_id_ten_digits",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    # This is the public sign-in ID, not the database primary key.  It remains a
    # string so IDs such as 0123456789 retain their leading zero.
    login_id = db.Column(db.String(10), unique=True, nullable=False, index=True)
    # Nullable only as a compatibility concession for accounts created before
    # Stage 1.  Registration always supplies a name.
    name = db.Column(db.String(100), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # Explicitly assigned only through a trusted database operation. Public
    # registration never accepts or sets this field.
    is_admin = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false(), index=True)
    owned_groups = db.relationship("Group", back_populates="owner", lazy=True, foreign_keys="Group.owner_id")
    memberships = db.relationship("GroupMembership", back_populates="user", lazy=True,
                                  cascade="all, delete-orphan")


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    archived_at = db.Column(db.DateTime, nullable=True, index=True)
    archived_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    owner = db.relationship("User", back_populates="owned_groups", foreign_keys=[owner_id])
    archived_by_user = db.relationship("User", foreign_keys=[archived_by_user_id])
    memberships = db.relationship("GroupMembership", back_populates="group", lazy=True,
                                  cascade="all, delete-orphan")
    members = db.relationship("Member", backref="group", lazy=True, cascade="all, delete-orphan")
    # Expense snapshots become financial history in Stage 6 and must never be
    # removed as an incidental consequence of deleting a group.
    expenses = db.relationship("Expense", backref="group", lazy=True)
    expense_records = db.relationship("ExpenseRecord", back_populates="group", lazy=True)

    @property
    def is_archived(self):
        return self.archived_at is not None


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)
    # Nullable: historical name-only members cannot be identified safely.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    user = db.relationship("User", foreign_keys=[user_id])


class GroupMembership(db.Model):
    """Authorization membership; separate from legacy expense ``Member`` rows."""

    __tablename__ = "group_membership"
    __table_args__ = (db.UniqueConstraint("group_id", "user_id", name="uq_group_membership_user"),)

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    group = db.relationship("Group", back_populates="memberships")
    user = db.relationship("User", back_populates="memberships")


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), default="")
    paid_by_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    # New expenses use this global identity; paid_by_id remains for legacy
    # compatibility during the transition.
    paid_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    expense_type = db.Column(db.String(20), default="equal")
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    paid_by = db.relationship("Member", foreign_keys=[paid_by_id])
    paid_by_user = db.relationship("User", foreign_keys=[paid_by_user_id])
    splits = db.relationship("ExpenseSplit", backref="expense", lazy=True, cascade="all, delete-orphan")


class ExpenseSplit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    amount_owed = db.Column(db.Numeric(12, 2), nullable=False)
    member = db.relationship("Member", foreign_keys=[member_id])
    user = db.relationship("User", foreign_keys=[user_id])


class ExpenseRecord(db.Model):
    """Stable identity and active pointer for an expense's immutable history."""

    __tablename__ = "expense_record"
    __table_args__ = (
        db.CheckConstraint(
            "(current_expense_id IS NOT NULL AND voided_at IS NULL AND voided_by_user_id IS NULL) OR "
            "(current_expense_id IS NULL AND voided_at IS NOT NULL AND voided_by_user_id IS NOT NULL)",
            name="ck_expense_record_active_or_voided",
        ),
        db.UniqueConstraint("current_expense_id", name="uq_expense_record_current_expense"),
    )

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id", ondelete="RESTRICT"), nullable=False, index=True)
    current_expense_id = db.Column(db.Integer, db.ForeignKey("expense.id", ondelete="RESTRICT"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    group = db.relationship("Group", back_populates="expense_records")
    current_expense = db.relationship("Expense", foreign_keys=[current_expense_id])
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])
    voided_by_user = db.relationship("User", foreign_keys=[voided_by_user_id])
    revisions = db.relationship("ExpenseRevision", back_populates="expense_record", lazy=True,
                                order_by="ExpenseRevision.version_number")


class ExpenseRevision(db.Model):
    """Audit metadata connecting one immutable snapshot to its logical root."""

    __tablename__ = "expense_revision"
    __table_args__ = (
        db.UniqueConstraint("expense_record_id", "version_number", name="uq_expense_revision_number"),
        db.UniqueConstraint("expense_id", name="uq_expense_revision_expense"),
        db.CheckConstraint("version_number > 0", name="ck_expense_revision_positive_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    expense_record_id = db.Column(db.Integer, db.ForeignKey("expense_record.id", ondelete="RESTRICT"), nullable=False,
                                  index=True)
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id", ondelete="RESTRICT"), nullable=False)
    version_number = db.Column(db.Integer, nullable=False)
    recorded_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    recorded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    expense_record = db.relationship("ExpenseRecord", back_populates="revisions")
    expense = db.relationship("Expense", foreign_keys=[expense_id])
    recorded_by_user = db.relationship("User", foreign_keys=[recorded_by_user_id])


class SettlementRequest(db.Model):
    __tablename__ = "settlement_request"
    __table_args__ = (
        db.CheckConstraint("requester_user_id != recipient_user_id", name="ck_settlement_request_distinct_users"),
        db.CheckConstraint("amount > 0", name="ck_settlement_request_positive_amount"),
        db.CheckConstraint("status IN ('PENDING', 'CONFIRMED', 'REJECTED')", name="ck_settlement_request_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    requester_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipient_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    resolved_at = db.Column(db.DateTime, nullable=True)
    requester = db.relationship("User", foreign_keys=[requester_user_id])
    recipient = db.relationship("User", foreign_keys=[recipient_user_id])


class ConfirmedSettlement(db.Model):
    """Append-only payment snapshot; application code never updates or deletes it."""

    __tablename__ = "confirmed_settlement"
    __table_args__ = (
        db.UniqueConstraint("settlement_request_id", name="uq_confirmed_settlement_request"),
        db.CheckConstraint("payer_user_id != recipient_user_id", name="ck_confirmed_settlement_distinct_users"),
        db.CheckConstraint("amount > 0", name="ck_confirmed_settlement_positive_amount"),
    )

    id = db.Column(db.Integer, primary_key=True)
    settlement_request_id = db.Column(db.Integer, db.ForeignKey("settlement_request.id", ondelete="RESTRICT"), nullable=False)
    payer_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    recipient_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    confirmed_at = db.Column(db.DateTime, nullable=False)
    settlement_request = db.relationship("SettlementRequest", foreign_keys=[settlement_request_id])
    payer = db.relationship("User", foreign_keys=[payer_user_id])
    recipient = db.relationship("User", foreign_keys=[recipient_user_id])
