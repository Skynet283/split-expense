# Stage 5: confirmed settlements

`confirmed_settlement` is created by the existing `db.create_all()` initialization.
Each row snapshots one request's payer, recipient, amount and UTC confirmation
time. Only these rows affect accounting; request statuses never do.

Existing Stage-4 CONFIRMED requests are left untouched and are NOT backfilled,
including on repeated confirmation. They require explicit historical
migration/reconciliation later. Startup performs no data conversion.

Confirmation and rejection claim a pending request with a conditional UPDATE,
then refresh it. On the configured SQLite database this obtains the writer lock
before obligation validation. The payment insert and request transition commit
once, with rollback on validation/database failure. Other database backends need
a separate review of concurrent confirmations of different requests for one pair.

Amounts retain Numeric(12, 2) / Decimal with exact-cent input validation and an
explicit 9,999,999,999.99 upper bound. SQLite Numeric storage itself does not
guarantee fixed-decimal storage for arbitrary external writes.

Immutability is application-scoped: no update/delete payment routes, no payment
delete cascades, no triggers and no ORM hooks. Direct SQL/ORM writes can still
change records. Foreign keys declare RESTRICT, but SQLite enforcement is not
enabled by this change. Broader history and database hardening are deferred.

Expense edits/deletions remain available. They can reverse a pair's current
obligation but never change an existing payment. Unresolved expenses are still
excluded; independent payments are retained. Group balances and exports remain
expense-only because payments have no group allocation. No Stage-6 functionality
is included.

The Stage-4 test expecting confirmed requests to leave balances unchanged is
updated to expect the Stage-5 payment effect; its authorization checks remain.
