# Stage 2 compatibility

New expenses use `User` IDs as their canonical payer and participant identities. During the transition, each new row also retains its linked `Member` ID because the existing expense tables require it.

`Member.user_id`, `Expense.paid_by_user_id`, and `ExpenseSplit.user_id` are nullable. The startup migration adds those columns without changing financial rows. It never maps a historical `Member` to a `User` by name. New groups record `Member.user_id` at creation. For an older group with memberships but no linked compatibility row, a new linked `Member` row is created only when a new global-user expense requires it; existing name-only members and their expenses are left unchanged.

`compute_user_balances(group)` reports only expense portions with complete User links. The existing `compute_balances(group)` remains the complete legacy-compatible balance view. The group page warns when legacy financial records cannot be included in the User-identity calculation.
