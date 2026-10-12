# Immediate settlements and audit-safe reversals

New payments take effect immediately. Either payer or recipient can record a
partial or full payment against an existing direct debtor-to-creditor obligation.
Both dashboard columns have amount inputs and Settle controls. Amounts use exact
cents and must be positive and no greater than the current obligation.

The existing `ConfirmedSettlement` snapshot remains the accounting source.
Each new payment atomically inserts an already-CONFIRMED `SettlementRequest` and
its payment snapshot. The request's requester is the debtor even when the
creditor records receipt. `recorded_by_user_id` identifies the actual recorder.
No new PENDING request or recipient approval is involved.

## Database additions

- Nullable `confirmed_settlement.recorded_by_user_id INTEGER`.
- Nullable `confirmed_settlement.submission_id VARCHAR(36)`.
- Unique index `uq_confirmed_settlement_submission` on `submission_id`.
- New `settlement_reversal` table: ID, unique `confirmed_settlement_id`,
  `reversed_by_user_id`, and `reversed_at`.

Startup uses the existing `initialize_database()` approach: `create_all()` for
missing tables, inspected additive ALTER TABLE operations, and an explicit
CREATE UNIQUE INDEX IF NOT EXISTS for existing payment tables. Repeated startup
is safe. Legacy payment metadata remains null; no recorder identity is guessed.
The expense-specific ActivityEvent schema needs no alterations or table rebuild.
No new dependencies are required; signing uses Flask's existing itsdangerous
dependency.

## Legacy records

| Record | Behavior after upgrade |
| --- | --- |
| PENDING request | Retained with its original status and metadata. Zero ledger effect, zero reservation, no blocking of payment creation or user deactivation. Displayed as an obsolete request. |
| REJECTED request without snapshot | Retained as historical only; zero accounting effect. |
| CONFIRMED request without snapshot | Retained without backfill; zero accounting effect, exactly as before. |
| Existing ConfirmedSettlement snapshot | Same payment effect as before until explicitly reversed; available in participant history and reversible by either participant. |

Payment snapshots are authoritative independently of the related request's
status or mutable request fields. The old confirm/reject POST endpoints remain
CSRF protected and authenticated, but return a retired-workflow message without
changing any request or creating any payment. Third parties receive 403.
The existing POST creation URL is retained as an immediate-payment alias;
all creations require a signed submission token. New dashboard forms use
`POST /settlements`.

## Reversal, accounting, and concurrency

`POST /settlements/<id>/reverse` permits either authenticated participant and
requires CSRF. It inserts one reversal with actor and UTC time. It never updates
or deletes the original request/payment, and never modifies an expense. A unique
settlement reference and a check under SQLite's writer lock prevent repeated or
concurrent reversals. Reversal and its audit metadata commit together.

Both global balance and direct pairwise helpers use the same active-payment
source, excluding snapshots with a reversal. They still read active expense
revisions across all groups, including archives, subtract payments before
reciprocal netting, and never infer partners from global net balances. Group
expense balances, group payment suggestions, and exports retain their existing
expense-only semantics. If expenses have changed since a payment, reversal
removes that payment from the current ledger; it does not restore an old expense
version.

Creation takes a SQLite writer claim before authoritative obligation validation
and holds it through the request/payment commit. Each rendered form contains a
signed random submission ID bound to recorder, payer, and recipient. Its unique
database index prevents repeated submission of the same partial-payment form,
even if debt remains. Replaying a reversed payment's form does not create a new
payment. A new payment requires a fresh form. Independently submitted payments
are revalidated under the writer lock to prevent overpayment. Database failures
roll back and return a retry message. These concurrency guarantees follow the
project's SQLite design.

Dashboard Settlement History and the existing Activity page share a display
macro. Activity combines expense events with payment, reversal, and unmatched
historical-request entries. Payments are visible only to their two participants,
even when another user can access a related group. History shows actor where
known, payer, recipient, amount, and UTC timestamp. Reversed payments remain
visible together with their reversal audit entry.

## PythonAnywhere deployment for the existing SQLite database

Use your current application's source directory, current virtualenv, and actual
deployed database path. The paths below are placeholders to replace once. The
local Windows paths are not PythonAnywhere paths. Preserve your deployed .env,
stable SECRET_KEY, WSGI configuration, and database file.

1. Open the PythonAnywhere Web tab. Note the source directory, virtualenv path,
   and WSGI configuration file. Identify the SQLite file selected by the deployed
   DATABASE_URL, or the project's `instance/expenses.db` default. If DATABASE_URL
   or SECRET_KEY is set in WSGI instead of .env, use the same values in the Bash
   console for this upgrade.
2. Stop application writes before changing code. If your Web tab offers Disable,
   disable the app. Alternatively, save a copy of your WSGI configuration outside
   its active path, temporarily replace it with the maintenance application below,
   and click Reload. Wait until the maintenance response is served; the original
   Flask workers must have stopped. Pause any other jobs writing this SQLite file.

   ```python
   def application(environ, start_response):
       body = b"Maintenance in progress. Please try again shortly."
       start_response("503 Service Unavailable", [
           ("Content-Type", "text/plain"),
           ("Content-Length", str(len(body))),
           ("Retry-After", "300"),
       ])
       return [body]
   ```

3. In a PythonAnywhere Bash console, set your real paths and activate the existing
   environment. Keep the database override in this console pointed at the existing
   deployed database throughout the upgrade.

   ```bash
   export PROJECT_DIR='/home/YOUR_USERNAME/YOUR_PROJECT_DIRECTORY'
   export VENV_DIR='/home/YOUR_USERNAME/YOUR_EXISTING_VIRTUALENV'
   export DB_PATH='/home/YOUR_USERNAME/YOUR_PROJECT_DIRECTORY/instance/expenses.db'
   cd "$PROJECT_DIR"
   source "$VENV_DIR/bin/activate"
   export DATABASE_URL="sqlite:///$DB_PATH"
   ```

4. Create a consistent SQLite backup before importing the new application. This
   checks the file exists and opens the source read-only, so a wrong path cannot
   silently create an empty database. Save the printed backup path.

   ```bash
   python - <<'PY'
   import os, sqlite3
   from contextlib import closing
   from datetime import datetime, timezone
   from pathlib import Path
   path = Path(os.environ['DB_PATH']).resolve(strict=True)
   stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
   backup = path.with_name(path.name + '.before-settlement-upgrade-' + stamp + '.bak')
   with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
       with closing(sqlite3.connect(backup)) as target:
           source.backup(target)
           assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
   print('Backup:', backup)
   PY
   ```

5. Upload/deploy the changed application files and templates, including the new
   `templates/_settlement_history.html`. Keep your existing database, .env, and
   virtualenv. No dependency update is needed for this change.
6. Run one serial initialization against the existing database. This uses the
   project's normal startup initializer when importing app.py; do not launch
   several consoles/workers to migrate at the same time. If it fails, keep the
   application in maintenance, correct the reported problem, and rerun this step.

   ```bash
   python - <<'PY'
   import os
   from pathlib import Path
   from config import Config
   path = Path(os.environ['DB_PATH']).resolve(strict=True)
   assert Config.SECRET_KEY, 'Configure the same stable SECRET_KEY used by WSGI before upgrading'
   assert Config.SQLALCHEMY_DATABASE_URI == 'sqlite:///' + str(path)
   from app import app
   from extensions import db
   from sqlalchemy import inspect, text
   with app.app_context():
       columns = {c['name'] for c in inspect(db.engine).get_columns('confirmed_settlement')}
       assert {'recorded_by_user_id', 'submission_id'} <= columns
       assert 'settlement_reversal' in inspect(db.engine).get_table_names()
       assert db.session.execute(text('PRAGMA integrity_check')).scalar() == 'ok'
   print('Additive settlement upgrade complete:', path)
   PY
   ```

7. Verify original rows against the backup before reopening the site. Set
   `BACKUP_PATH` to the exact path printed in step 4. The following compares every
   original column in the listed tables; new columns and the new reversal table
   do not count as changes to old data. This verification imports no app and
   opens both databases read-only.

   ```bash
   export BACKUP_PATH='/home/YOUR_USERNAME/YOUR_PROJECT_DIRECTORY/instance/expenses.db.before-settlement-upgrade-TIMESTAMP.bak'
   python - <<'PY'
   import os, sqlite3
   from contextlib import closing
   from pathlib import Path
   tables = ('users', 'group', 'group_membership', 'member', 'expense', 'expense_split',
             'expense_record', 'expense_revision', 'activity_event',
             'settlement_request', 'confirmed_settlement')
   old_uri = Path(os.environ['BACKUP_PATH']).resolve(strict=True).as_uri() + '?mode=ro'
   new_uri = Path(os.environ['DB_PATH']).resolve(strict=True).as_uri() + '?mode=ro'
   with closing(sqlite3.connect(old_uri, uri=True)) as before, closing(sqlite3.connect(new_uri, uri=True)) as after:
       for table in tables:
           columns = [row[1] for row in before.execute(f'PRAGMA table_info("{table}")')]
           if not columns:
               continue
           names = ', '.join('"' + c.replace('"', '""') + '"' for c in columns)
           query = f'SELECT {names} FROM "{table}" ORDER BY id'
           assert before.execute(query).fetchall() == after.execute(query).fetchall(), table + ' changed'
           print(table + ': original rows preserved')
   PY
   ```

8. Restore the original WSGI configuration if using the maintenance application,
   or re-enable the app if disabled, then click Reload on the Web tab. Check the
   error log and sign in to verify both settlement columns, legacy request labels,
   and history. Existing balances should match the pre-upgrade balances, with
   previously reserved pending amounts available again. Only record a real
   payment, and only reverse a payment when appropriate.

PythonAnywhere documents virtualenv selection and Web-tab reload behavior here:
[virtualenv setup](https://help.pythonanywhere.com/pages/VirtualEnvForWebsites/),
[reloads](https://help.pythonanywhere.com/pages/ReloadWebApp/), and
[environment configuration](https://help.pythonanywhere.com/pages/environment-variables-for-web-apps/).
The temporary maintenance WSGI application is the release procedure above, not
a change to the expense application's accounting or normal user flow.

The upgrade was tested on disposable old-schema SQLite fixtures. The actual
local/deployed expense database was not upgraded during development.

## Changed files and validation

Application: `models.py`, `app.py`, `helpers.py`, `routes.py`, `activity.py`,
`templates/index.html`, `templates/activity.html`, and new
`templates/_settlement_history.html`.

Test fixtures and tests: `test_factory.py`, `tests/test_global_settle_up.py`,
`tests/test_security.py`, `tests/test_stage4.py`, `tests/test_stage5.py`,
`tests/test_stage6.py`, `tests/test_stage7.py`, `tests/test_stage9.py`,
`tests/test_stage10.py`, `tests/test_stage13.py`, `tests/test_stage14.py`,
`tests/test_stage14_group_suggested_payments.py`, and new
`tests/test_immediate_settlements.py`.

Documentation: `USER_GUIDE.md` and this new `SETTLEMENT_UPGRADE.md`.

The complete suite passed on 2026-10-04: **139 tests in 81.515 seconds, OK**.
The final run used deprecation warnings as errors, in-memory fixture databases,
and disposable file-backed SQLite databases for migration/concurrency tests.
The default app imported by tests was explicitly redirected to memory:

```powershell
$env:DATABASE_URL = 'sqlite://'
$env:INITIAL_ADMIN_ID = ''
$env:INITIAL_ADMIN_PASSWORD = ''
.\myenv\Scripts\python.exe -W error::DeprecationWarning -m unittest discover -s tests -v
```

Coverage includes debtor/creditor full and partial payments, immediate effects,
both rendered controls and an actual creditor-form submission, participant-only
history, reversal preservation/authorization/CSRF/rollback/uniqueness,
overpayment/invalid amounts, signed-form replay protection, concurrent opposite
participants, concurrent duplicate submissions, concurrent reversals,
payment-versus-expense-correction serialization, legacy requests and payment
snapshots, repeatable real old-schema upgrades, archived/multiple groups,
reciprocal obligations, and pending-request neutrality for user deactivation.
Existing expense revision, void, archive, activity, user-management, group-picker,
share-allocation, export, and security regressions also pass. `git diff --check`
passed. No production deployment was performed.
