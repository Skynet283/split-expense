# Stage 10 release audit

Audit date: 2026-09-12. Baseline: Stages 1–9, 53 passing tests.
Final result: 64 passing tests, including 11 new release integration tests.
No Stage 11 functionality, new financial model, migration framework, or ORM refactor was introduced.

## Verdict

**Release-ready with deployment caveats.** The tested SQLite accounting,
authorization, and CSRF-protected mutation flows pass. Production secret
configuration, existing bootstrap-account credential rotation, and the
operational limitations below still need attention before deployment.

## Files changed in this stage

The repository already contained uncommitted Stage 1–9 work. That work was
preserved; the complete Git diff is not a Stage 10-only diff.

| File | Stage 10 change |
| --- | --- |
| `app.py` | Explicit bootstrap configuration; no implicit owner assignment without a configured account; random fallback session secret; SQLite backfill writer claim; debug disabled for direct execution |
| `config.py` | Removed shipped bootstrap ID/password and predictable session-key defaults |
| `helpers.py` | Reject non-finite expense amounts; removed unused `Expense` import |
| `routes.py` | Archive/write serialization, pending-request serialization, write-error rollback, safer login redirect, literal text in Excel exports |
| `templates/index.html` | Removed user-controlled group name from inline JavaScript |
| `templates/group.html` | Removed add-expense empty-state link from archived groups |
| `templates/add_expense.html` | Corrected total-amount labels pointing to the wrong form control |
| `tests/test_stage1.py`, `tests/test_stage8.py` | Replaced the two legacy `Query.get()` calls with `db.session.get()` |
| `tests/test_stage10.py` | New release integration tests |
| `STAGE10_AUDIT.md` | This report |

`models.py`, `expense_history.py`, dependency requirements, and the financial
calculation algorithms were not changed in Stage 10. No database schema was added.

## Findings and fixes

1. **Non-finite expense input:** `Decimal('NaN').quantize(...)` returns NaN,
   then comparison raises `InvalidOperation` outside the parser's exception
   handler. It could produce a server error during add/edit. The parser now
   rejects non-finite values as validation errors. Tests also cover signaling
   NaN, infinities, and excessively large exponents, checking full rollback.
2. **Archive/write race:** add/edit/void previously trusted an archive check made
   before the write lock. Archive could commit between that check and the write.
   All three paths now conditionally claim the active owned group within their
   write transaction. Tests force archive to commit in precisely that interval
   and require a 403 with no new, changed, or voided financial rows.
3. **Archive metadata race:** archive now uses a conditional update, preserving
   the first successful archive metadata when another request loaded an older
   active state.
4. **Pending-request race:** two creations could validate the same available
   amount and jointly exceed it. Creation now obtains the SQLite writer lock
   through a no-op requester-row update before validating pending totals and
   inserting. Concurrent 300.00 requests against 500.00 result in one request.
5. **Uncovered database failures:** registration, group creation, request creation,
   and archive lacked explicit write-error handling. They now rollback, log the
   failure, and give a non-sensitive response. Registration also handles a
   duplicate-ID race. Failure injection after flush verifies complete recovery
   for these paths and expense add/edit/void; confirmation rollback was already
   covered by Stage 5.
6. **Shipped credentials:** default configuration contained a fixed bootstrap
   login/password and predictable session secret. Those defaults are removed.
   Bootstrap creation requires an explicit valid ID and password; it still
   creates a non-admin account. Existing hashes and roles are never reset.
   Without `SECRET_KEY`, local startup uses a random ephemeral key and logs a
   warning. Direct execution no longer unconditionally enables debug mode.
7. **Redirect validation:** a backslash or control character in a slash-prefixed
   `next` value could be interpreted differently by a browser. Such values now
   fall back to the index; ordinary local paths and queries still work.
8. **Inline JavaScript:** HTML escaping alone does not safely embed a group name
   inside an event handler's JavaScript string. Archive confirmation now uses
   static wording. A hostile group name is tested against decoded HTML handlers.
9. **Excel formulas:** user-entered descriptions/names beginning with `=` could
   become formulas in openpyxl exports. This workbook has no intended formulas;
   formula-typed cells are now saved as literal strings, preserving their text
   and all numeric values. The exported workbook is reopened in a regression test.
10. **Archived empty-state UI:** an owner still saw an add-expense link for an
    archived group with no active expenses. The link now follows the same archive
    restriction as the main action. A misplaced amount-label association was
    also corrected.
11. **Backfill race window:** SQLite backfill now uses `BEGIN IMMEDIATE` before
    reading which snapshots already have revisions. This prevents a concurrent
    expense writer or backfill from changing that set during the wrapping batch.
    The batch still commits once and rolls back completely on failure.

## Financial data flow and invariant review

`User` provides identity; `GroupMembership` provides group participation.
New `Expense` snapshots retain explicit User links and required legacy Member
links. `ExpenseRecord.current_expense_id` selects one snapshot; `ExpenseRevision`
preserves ordered versions. `SettlementRequest` is workflow only.
`ConfirmedSettlement` is the immutable application payment snapshot. Global and
pairwise accounting read current expenses and independent confirmed payments.

| Invariant | Evidence |
| --- | --- |
| Global balances sum to zero | Stages 3/5 and Stage 10 multi-group lifecycle; Stage 10 also reconstructs every user's balance from pairwise obligations |
| Active expenses count exactly once | Unique current pointers, canonical `iter_active_expenses()`, Stage 6 current-accounting/export tests |
| Historical revisions do not double-count | Stage 6 revisions and Stage 10 correction lifecycle |
| Voided expenses contribute zero | Stage 6 void tests and Stage 10 correction/void/archive lifecycle |
| Confirmed payments count exactly once | Unique request constraint, independent payment query, repeated/concurrent resolution tests |
| Pending/rejected requests contribute zero | Stage 4/5 tests; no request-state contribution in either accounting helper |
| Overpayment reverses a direct obligation | Stage 5/6 and Stage 10 correction/void lifecycle |
| Corrections preserve confirmed payments | Stage 6 and Stage 10 comparison of every persisted payment column |
| Archived groups stay in accounting | No archive filter in active-expense source; Stage 7 and Stage 10 |
| Unmapped legacy rows are excluded and reported | Stage 3/5 and Stage 10 mixed legacy/modern fixtures |
| No identity mapping by name | Explicit IDs throughout; same-name unmapped Member in Stage 10 remains unmapped |
| Cent-level precision | Decimal calculation and stored cent values; Stage 3/5 plus Stage 10 allocation of 0.05 as 0.03 and 0.02 |

Pairwise obligations come only from expense payer/participant relationships and
confirmed payment pairs, never from global balance inference. Reciprocal pairs
net, payments reduce the correct direction, and excess payment reverses it.
Group balances, suggested group payments, and exports intentionally remain
expense-only: confirmed payments have no group allocation. The older
`compute_user_balances(group)` helper retains its documented partial-link
compatibility semantics; global and pairwise accounting exclude incomplete
expenses in full. Expense input retains existing HALF_UP cent rounding;
settlement input requires exact cents. Neither policy was redesigned.

## Authorization and immutable history

All financial mutation routes require authentication. Expense creation,
revision, void, and archive require group ownership. Existing membership grants
read/history/export access, not ownership. Cross-group record IDs return 404;
known but unauthorized groups return 403. Anonymous protected requests redirect
to login. POST-only mutation endpoints reject GET.

Only the request recipient may confirm/reject, and only PENDING requests may
transition. Repeated resolution does not insert another payment. Stale requests
stay pending without recording a payment. The conditional claim, payment insert,
and workflow transition are one transaction.

Admin is explicit `is_admin`; registration ignores attempted role injection.
The read-only admin endpoint has no mutation route and does not bypass group
ownership or recipient rules. Password hashes are not rendered. Authentication
uses Werkzeug password hashing; password/confirmation equality is form
validation, not a raw stored-password authentication check.

Edits append complete immutable snapshots; identical submissions create no
revision. The no-op implementation deletes only its newly created, unreferenced
candidate, never a historical expense. Void clears the pointer and stores
actor/time. No route updates/deletes historical snapshots or confirmed payments.
Archive never deletes groups, memberships, expenses, revisions, or settlements.

## Migration and backfill

Additive startup upgrades remain repeatable. The upgrade code adds nullable
identity/archive fields and `is_admin DEFAULT 0`, repairing null admin flags
without promoting accounts. Existing users, hashes, User/Member links, expense
IDs, descriptions, amounts, splits, and source timestamps are preserved.
Ownerless groups now require an explicitly configured bootstrap account; they
are not silently assigned to a default identity. Existing configured accounts
are reused without password resets.

`db.create_all()` creates missing history/request/payment tables but does not
repair arbitrary schema drift or add missing constraints/indexes to old tables.
Individual additive changes commit separately: this is not a single atomic
whole-database migration. On failure, completed additive changes remain and can
be retried. The expense wrapping batch is atomic. A failure on its second
revision leaves no partially wrapped roots; repeated retry completes exactly once.
New history metadata may use a database default when a legacy source timestamp
is null; the original snapshot remains unchanged.

A genuine pre-upgrade SQLite fixture exercises missing user name/admin,
group owner/archive, and member/expense/split User-link columns, plus missing
modern tables. Repeated complete initialization preserves the original expense
row and creates a single revision/membership. Stage 8 separately covers legacy
user migration, and Stage 6 verifies exact split/identity backfill data.

Historical Stage 4 CONFIRMED requests are deliberately not converted into
ConfirmedSettlement payments. They require operator reconciliation, as Stage 5
already documented. This behavior remains covered by its regression test.

Run upgrades once with traffic stopped before starting multiple workers. The
backfill writer claim does not make the earlier `create_all`/ALTER/bootstrap
phases safe for simultaneous startup. Back up the database and validate a copy
first. Importing `app.py` still initializes its configured database for existing
WSGI compatibility; tests redirected the default import database to memory.

## Concurrency

SQLite serializes writers. Conditional claims are acquired before authoritative
validation and held through commit/rollback. Unique revision numbers and unique
confirmed-request IDs provide additional database constraints. Existing tests
cover simultaneous revisions and simultaneous confirmations of different
requests; new file-backed, separate-connection tests cover request creation,
archive versus each expense mutation, and confirm versus correction.

For confirm/correction, either serial outcome is valid: correction first makes
the request stale; confirmation first preserves that payment while the correction
may reverse the obligation. The test requires the corresponding workflow,
history, payment count, and final balances to agree.

This is SQLite-specific concurrency assurance. PostgreSQL/MySQL row locks do not
provide the same global writer serialization across different requests and
expense records. No claim of cross-backend correctness is made. SQLite lock
timeouts can still reject a write with a retry message; there is no automatic
retry framework or stale-browser edit conflict UI.

## SQLite foreign-key decision

`PRAGMA foreign_keys = ON` was **not enabled**. The migration test verifies the
existing setting remains off. Old additive columns lack actual FK constraints,
and existing deployments may contain references created while enforcement was
off. Enabling it globally would not retrofit those constraints and could change
legacy upgrade/external-maintenance behavior without a repair policy.

Before enabling it in a separate database-hardening change, inspect declared
constraints and orphan references on a backup, run `foreign_key_check`, resolve
violations, plan any required table rebuilds, and test enforcement on every
pooled connection. CHECK/UNIQUE constraints on newly created tables are still
enforced independently of this pragma. Application authorization/transactions
preserve history through supported routes; direct database writes can still
mutate or orphan it. No triggers or generic ledger were added.

## Validation

`python` is absent from this PowerShell PATH; commands used the project's
`myenv\Scripts\python.exe`. The sandbox could not access its underlying Python
installation, so execution used the approved escalation. Tests used in-memory
and temporary SQLite databases; the existing expense database was not migrated.

The final full suite used the requested discovery command with the stronger
`-W error::DeprecationWarning` option:

```text
.\myenv\Scripts\python.exe -W error::DeprecationWarning -m unittest discover -s tests -v
----------------------------------------------------------------------
Ran 64 tests in 36.361s

OK
```

All 53 existing tests and 11 new Stage 10 tests passed. The two known
SQLAlchemy `Query.get()` warnings are gone. The only startup warning in the run
was the intentional unset-SECRET_KEY warning for the isolated default app.
The initial Stage 10 run had one test-fixture connection-cleanup error on
Windows; the fixture now explicitly closes its SQLite connection and the final
suite passed.

`git diff --check`: exit 0, no whitespace errors; Git emitted LF-to-CRLF notices
for existing tracked working copies. New files were checked separately because
`git diff --check` does not inspect untracked files.

```text
.\myenv\Scripts\python.exe -m compileall -q -x '(^|[\\/])(myenv|\.git)([\\/]|$)' .
```

Compileall: exit 0, no errors. Project source and tests were compiled; the virtual
environment and Git metadata were excluded.

## Intentionally unchanged and remaining deployment work

- CSRF protection now requires a session-bound token on every unsafe request and
  supplies it in every rendered POST form. It accepts an `X-CSRF-Token` header
  for a future same-origin JavaScript client. Token validation, forged/missing
  rejections, login/logout rotation, and form coverage are tested.
- Set a strong stable `SECRET_KEY`. The ephemeral fallback is for local use;
  restarts invalidate sessions and separate worker keys would disagree.
- Removing credential defaults does not rotate already-created accounts. An
  operator must change any existing known bootstrap password and any previously
  deployed predictable session key. No real user's credentials were changed here.
- Use a production WSGI server, HTTPS, appropriate secure-cookie/proxy settings,
  restricted database/backup/log access, and backups. The Flask development
  server is not a production deployment configuration.
- Dependencies remain unpinned. Dependency vulnerability scanning, rate limiting,
  account-abuse controls, live browser verification, and load testing were not
  performed. Bootstrap CSS/icons still depend on external CDN availability.
- SQLAlchemy Numeric on SQLite is not a fixed-decimal guarantee for arbitrary
  external writes. History immutability is application-scoped; no direct-SQL
  tamper protection or full FK enforcement exists.
- Keep startup upgrades serial; old tables do not automatically receive every
  modern index/constraint. Arbitrarily damaged databases need explicit repair.
- Group suggestions/exports are expense-only; legacy confirmed requests without
  payment snapshots need reconciliation. These existing compatibility policies
  were preserved rather than inventing financial history.
- The current UI can show and resolve settlement requests but has no request
  creation form, and voided-history navigation is limited to known history URLs.
  Those are existing usability limitations, not new Stage 10 features.

No production deployment was performed. Earlier stage compatibility documents
describe their historical stage boundaries; this report records the final audit
and supersedes their earlier bootstrap-default behavior.
