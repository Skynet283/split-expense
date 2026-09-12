# Stage 6: immutable expense history

`ExpenseRecord` is the stable logical identity of an expense. Its nullable
`current_expense_id` selects the one active `Expense` snapshot. Every immutable
snapshot and its immutable `ExpenseSplit` rows are linked through an ordered
`ExpenseRevision`. Voiding clears the active pointer and records the actor/time;
it does not delete a snapshot.

Startup creates the two new tables, then transactionally and idempotently wraps
each pre-Stage-6 `Expense` as revision 1. Existing payer, Member/User links,
amount, description, split rows and timestamps are not reinterpreted or changed.
Legacy/unmapped snapshots remain preserved and excluded from global accounting.

All current expense calculations and displays use `iter_active_expenses()` (or
its logical-record counterpart). Historical and voided snapshots are excluded;
`ConfirmedSettlement` continues to be counted independently and exactly once.

Edits claim the active logical root, build a complete new snapshot, compare it
with the current snapshot, and append the next uniquely numbered revision in one
transaction. Identical submissions create no revision. The conditional root
update serializes SQLite writers; the database uniqueness constraints reject
duplicate version numbers and reuse of a snapshot.

The former expense delete action now voids. Groups with any financial history
cannot be physically deleted; empty groups can. History is application-immutable:
there are no mutation routes, triggers, or ORM mutation hooks. Foreign-key
enforcement and protection from direct database writes remain deployment risks.
Group archival and all Stage-7 behavior are intentionally absent.
