import logging
from io import BytesIO

import openpyxl
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from helpers import ZERO, compute_balances, compute_settlements, save_splits
from models import Expense, ExpenseSplit, Group, Member

main = Blueprint("main", __name__)
logger = logging.getLogger(__name__)


def group_member_or_error(group, member_id):
    member = db.session.get(Member, member_id)
    if member is None or member.group_id != group.id:
        raise ValueError("The selected payer must belong to this group.")
    return member


@main.route("/")
def index():
    return render_template("index.html", groups=Group.query.order_by(Group.id.desc()).all())


@main.route("/groups", methods=["POST"])
def create_group():
    name = request.form.get("name", "").strip()
    member_names = list(dict.fromkeys(member.strip() for member in request.form.get("members", "").split(",") if member.strip()))
    if not name or len(member_names) < 2:
        flash("Enter a group name and at least two distinct members.", "danger")
        return redirect(url_for("main.index"))
    if len(name) > 100 or any(len(member) > 100 for member in member_names):
        flash("Group and member names must be at most 100 characters.", "danger")
        return redirect(url_for("main.index"))

    group = Group(name=name)
    db.session.add(group)
    db.session.flush()
    db.session.add_all(Member(name=member_name, group_id=group.id) for member_name in member_names)
    db.session.commit()
    flash(f'Group "{name}" created!', "success")
    return redirect(url_for("main.view_group", group_id=group.id))


@main.route("/groups/<int:group_id>")
def view_group(group_id):
    group = db.get_or_404(Group, group_id)
    balances = compute_balances(group)
    return render_template("group.html", group=group, balances=balances,
                           settlements=compute_settlements(balances),
                           member_map={member.id: member.name for member in group.members}, zero=ZERO)


@main.route("/groups/<int:group_id>/expenses/new", methods=["GET", "POST"])
def add_expense(group_id):
    group = db.get_or_404(Group, group_id)
    if request.method == "POST":
        try:
            paid_by_id = int(request.form.get("paid_by", ""))
            group_member_or_error(group, paid_by_id)
            expense = Expense(description=request.form.get("description", "").strip(), paid_by_id=paid_by_id,
                              amount=ZERO, expense_type=request.form.get("expense_type", "equal"), group_id=group.id)
            db.session.add(expense)
            db.session.flush()
            save_splits(expense, group.members, expense.expense_type, request.form, current_app.config["MAX_EXPENSE_AMOUNT"])
            db.session.commit()
            flash("Expense added!", "success")
        except (ValueError, TypeError) as error:
            db.session.rollback()
            flash(str(error), "danger")
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Could not add expense to group %s", group_id)
            flash("We could not save that expense. Please try again.", "danger")
        return redirect(url_for("main.view_group", group_id=group.id))
    return render_template("add_expense.html", group=group)


@main.route("/groups/<int:group_id>/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def edit_expense(group_id, expense_id):
    group = db.get_or_404(Group, group_id)
    expense = db.get_or_404(Expense, expense_id)
    if expense.group_id != group.id:
        return redirect(url_for("main.view_group", group_id=group.id))
    if request.method == "POST":
        try:
            paid_by_id = int(request.form.get("paid_by", ""))
            group_member_or_error(group, paid_by_id)
            expense.description = request.form.get("description", "").strip()
            expense.paid_by_id = paid_by_id
            expense.expense_type = request.form.get("expense_type", "equal")
            ExpenseSplit.query.filter_by(expense_id=expense.id).delete()
            db.session.flush()
            save_splits(expense, group.members, expense.expense_type, request.form, current_app.config["MAX_EXPENSE_AMOUNT"])
            db.session.commit()
            flash("Expense updated!", "success")
        except (ValueError, TypeError) as error:
            db.session.rollback()
            flash(str(error), "danger")
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Could not update expense %s", expense_id)
            flash("We could not save those changes. Please try again.", "danger")
        return redirect(url_for("main.view_group", group_id=group.id))
    current_splits = {split.member_id: split.amount_owed for split in expense.splits}
    return render_template("edit_expense.html", group=group, expense=expense,
                           current_splits=current_splits, shared_ids=list(current_splits))


@main.post("/groups/<int:group_id>/expenses/<int:expense_id>/delete")
def delete_expense(group_id, expense_id):
    expense = db.get_or_404(Expense, expense_id)
    if expense.group_id != group_id:
        return redirect(url_for("main.view_group", group_id=group_id))
    db.session.delete(expense)
    db.session.commit()
    flash("Expense deleted.", "info")
    return redirect(url_for("main.view_group", group_id=group_id))


@main.post("/groups/<int:group_id>/delete")
def delete_group(group_id):
    group = db.get_or_404(Group, group_id)
    name = group.name
    db.session.delete(group)
    db.session.commit()
    flash(f'Group "{name}" deleted.', "info")
    return redirect(url_for("main.index"))


@main.route("/groups/<int:group_id>/export")
def export_excel(group_id):
    group = db.get_or_404(Group, group_id)
    balances = compute_balances(group)
    settlements = compute_settlements(balances)
    member_map = {member.id: member.name for member in group.members}
    workbook = openpyxl.Workbook()
    expenses_sheet = workbook.active
    expenses_sheet.title = "Expenses Log"
    expenses_sheet.append(["#", "Date", "Description", "Paid By", "Amount (₹)", "Split Among", "Type"])
    for index, expense in enumerate(group.expenses, 1):
        expenses_sheet.append([index, expense.created_at, expense.description or "", expense.paid_by.name,
                               expense.amount, ", ".join(f"{member_map[split.member_id]} (₹{split.amount_owed:.2f})" for split in expense.splits),
                               expense.expense_type.capitalize()])
    balances_sheet = workbook.create_sheet("Final Balances")
    balances_sheet.append(["Member", "Balance (₹)", "Status"])
    for member in group.members:
        balance = balances.get(member.id, ZERO)
        balances_sheet.append([member.name, abs(balance), "Settled" if balance == ZERO else "Should Receive" if balance > ZERO else "Should Pay"])
    payments_sheet = workbook.create_sheet("Suggested Payments")
    payments_sheet.append(["From (Debtor)", "To (Creditor)", "Amount (₹)"])
    for debtor_id, creditor_id, amount in settlements:
        payments_sheet.append([member_map[debtor_id], member_map[creditor_id], amount])
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{group.name.replace(' ', '_')}_expenses.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
