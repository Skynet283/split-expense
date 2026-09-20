# Expense Splitter — User Guide

## 1. Overview

Expense Splitter helps registered users share costs in groups. A group records who paid, who shared each expense, and the resulting balances. A user can belong to more than one group, and the **Global Settle Up** area brings together direct amounts owed across those groups. Expense revisions, settlement requests, confirmed payments, and expense activity remain available as history.

## 2. Logging In

On the sign-in page, enter your **10-digit User ID** and password. Ask the administrator for an account if you do not have one: public registration is disabled. If your account is deactivated, you cannot sign in; the same generic invalid-credentials message is shown as for an incorrect ID or password. Contact the administrator if you need access restored or a password reset.

## 3. Creating a Group

From **Groups**, find **Create New Group**:

1. Enter a group name.
2. Under **Add registered users**, browse the scrollable list or type part of a name or User ID. Name searching is case-insensitive, and the list filters as you type.
3. Select one or more people. They appear as removable chips under **Selected**; use a chip's × button to remove someone before submitting.
4. Select **Create Group**. You are added automatically as the group's owner and a member, even if you select nobody else.

Only active registered users other than yourself appear in the picker. A deactivated or invalid User ID cannot be added by submitting it manually.

## 4. Adding Expenses

Any **active member** of an unarchived group can open that group and select **Add Expense**. Enter an optional description, choose an active group member under **Paid By**, then choose a split:

- **Equal Split:** enter the total and check the active people who shared it. The amount is divided among those selected.
- **Custom Split:** enter the total and each active person's amount. The individual amounts must add up to the total.
- **Split by Shares:** enter the total and a share weight for each participating active person. Weights may be positive whole numbers or decimals. Enter `0` to include someone who owes ₹0, or leave their field blank to exclude them. At least one weight must be greater than zero. The form previews the calculated amounts; the server calculates and stores the final cent amounts when you submit.

The payer and participants must belong to the group and be active. Inactive members are absent from the new-expense form; manually submitted inactive IDs are rejected. Invalid amounts or participants do not create a partial expense. A successful expense appears in the group's expense log and affects balances.

## 5. Editing Expenses

Only the person who **created** an expense can select **Edit** and save a new revision. Other authorized group members can still view the expense and its history. Editing does not silently overwrite the previous version: the earlier version remains in **History**, while the new version becomes current.

An expense involving a deactivated user cannot be revised while that user is inactive. It remains viewable; after reactivation, the creator can revise it again. Archived groups and voided expenses cannot be edited.

## 6. Expense / Revision History

Select **History** beside an expense to see its versions, amounts, payer, shares, and recorded dates. The current version is marked. If the group owner **voids** an expense, its prior versions remain visible with a void notice, but it no longer appears as an active expense. Only the owner can void an expense in an active group.

## 7. Global Activity History

Select **Activity** in the top navigation to see expense additions and revisions across all groups **you are authorized to access**, including activity in archived groups. Entries link to the group and expense history. “Global” does **not** mean you can see every group's transactions. This feed currently does not list settlement, admin-management, or expense-void events.

## 8. Balances

Each group shows **Group Expense Balances**. Its **Suggested Expense Payments** card is hidden by default but may be enabled by the application operator. The dashboard's **Global Settle Up** remains available regardless of that card and combines direct obligations across groups, including archived groups, and confirmed settlements. You may owe one person while another person owes you, even when your overall net position is zero. Older expenses without a reliable registered-user link remain in the group's legacy balance view but are not assigned to a global User by guessing a name.

## 9. Global Settle Up

On **Groups**, the **You owe** and **Owed to you** columns show direct amounts between you and other users. To propose a payment, choose **Settle** on an available **You owe** entry; you can adjust the amount within the available limit. The request appears under **Settlement Requests** as **Pending**.

A pending request reserves that amount against further requests for the same direct obligation, but **it is not a completed payment** and does not reduce the ledger balance. The recipient can **Confirm payment** or **Reject request**. Confirmation records the payment and reduces the relevant outstanding obligation; the confirmed payment is preserved as an immutable historical record. Rejection releases the reserved amount without recording a payment. If the underlying obligation changes, a stale request may fail confirmation rather than create an invalid payment.

## 10. Archived Groups

Only the group owner can select **Archive** from the active group list. Archiving removes the group from that active list and makes it read-only: no new expenses, revisions, or voids are allowed. Group members can still view an archived group's financial and expense history through an existing link or its group address, and the group can still be exported. Its historical transactions and confirmed settlements continue to affect global calculations.

# Administrator Guide

## 11. Admin Login / Setup

The administrator signs in through **Administrator sign in** on the normal sign-in page, or at `/admin/login`. This is a separate backend-controlled login, not a normal user account.

Before starting the app, create a `.env` file in the project root with these settings:

- `ADMIN_USERNAME`: the administrator's chosen username.
- `ADMIN_PASSWORD`: the administrator's chosen password.

For example, replace both placeholders with your own values:

```dotenv
ADMIN_USERNAME=<chosen-admin-username>
ADMIN_PASSWORD=<chosen-admin-password>
```

The app loads `.env` automatically at startup; values already set in the process environment take precedence. Both settings must be present for administrator sign-in. Unlike normal user passwords, this local administrator password is stored as plaintext in `.env`: keep the file private, never share it, and do not commit it to Git. The repository ignores `.env`. The application also needs a stable `SECRET_KEY` for sessions; production mode requires it. The older `INITIAL_ADMIN_ID` bootstrap setting creates a **normal user**, not this administrator.

The application setting `SHOW_GROUP_SUGGESTED_PAYMENTS` in `config.py` defaults to `False`, hiding the suggested-payment card inside individual groups. Set it to `True` to show that card using the existing suggestions. This setting does not change **Global Settle Up**, balances, or settlements.

## 12. Admin Dashboard

After administrator sign-in, open **User Management** (the `/admin` page). It shows global balances, a financial-health warning when active expenses lack a reliable user link, a list of users with active/deactivated status and group counts, and a group summary. The database's legacy `is_admin` flag does not grant access to this control plane. Administrator sign-in does not grant permission to create or edit expenses as a group member.

## 13. Creating Users

Only the administrator can create normal user accounts through the application interface:

1. Open **User Management** and find **Create user**.
2. Enter the person's name, a unique **10-digit User ID**, an initial password, and the same password again for confirmation.
3. Select **Create user**. The account is active and can sign in with that ID and password.

The page rejects missing or mismatched passwords, invalid IDs, and duplicate IDs. Normal users cannot self-register.

## 14. Resetting a User Password

In the user's row, enter a **New password**, enter it again under **Confirm password**, then select **Reset Password**. The old password stops working and the new one is used for future sign-ins. The administrator cannot view or recover the old password; user passwords are stored as hashes, not readable text.

## 15. Deactivating Users

Use **Deactivate** in the user's row when access should be frozen without deleting financial history. Deactivation succeeds only if **all three** conditions hold:

1. The user's global net balance is zero.
2. No direct outstanding obligation involves the user, as debtor **or** creditor.
3. No pending settlement request involves the user.

Zero net balance alone is not enough: someone may owe one person ₹100 while another person owes them ₹100. The outstanding relationships and pending requests must also be resolved first. If a condition fails, the user stays active and the page shows why.

A deactivated user cannot sign in or be selected for a new group or new expense. Existing group memberships, expenses, revisions, settlements, activity, and accounting history remain intact. Expenses already involving that user are viewable but cannot be revised while they are inactive. Deactivation never deletes the user or automatically settles anything. Legacy name-only expenses are not linked to a User by guessing an identity.

## 16. Reactivating Users

In a deactivated user's row, select **Reactivate**. This restores sign-in and eligibility for future groups and expense participation using the **same account**. Existing memberships and financial history are unchanged.

## 17. What Admin Cannot / Should Not Do

User Management does not reveal plaintext passwords, delete users, or grant arbitrary rights to alter group expenses and settlements. Do not edit financial database rows manually to bypass the app's checks; use the existing group and settlement workflows so the audit and accounting history remain coherent.

## Quick Reference

| Task | Who can do it? |
| --- | --- |
| Sign in as a normal user | Active registered user |
| Create or reset a user account | Administrator only |
| Create a group | Active signed-in user |
| Add an expense | Active member of an unarchived group |
| Edit an expense | Its creator, while the group is active and no current participant is deactivated |
| View group expense history | Group owner or member |
| View relevant global activity | Signed-in user, for groups they can access |
| Request a settlement | Signed-in user with a valid direct amount owed and room beyond pending requests |
| Confirm or reject a pending settlement | Its recipient |
| Archive a group or void its active expense | Group owner |
| Deactivate or reactivate a user | Administrator only |

## Troubleshooting

- **Cannot sign in?** Check the 10-digit ID and password. If the account is deactivated or the password was reset, ask the administrator.
- **Someone is missing from the group picker?** They may be deactivated, or they may be the current user (who is added automatically). Ask the administrator to check their status.
- **Someone is missing from a new-expense form?** Only active members of that group can be payer or participant. Reactivation restores eligibility without removing history.
- **Cannot edit an expense?** Only its creator can revise it. Voided expenses, archived groups, and expenses involving a currently deactivated participant cannot be revised.
- **Cannot deactivate a user?** Resolve their global balance, all direct obligations, and all pending settlement requests first.
- **A settlement is still pending?** The recipient has not confirmed or rejected it. Pending amounts reserve availability but are not completed payments.
- **Admin sign-in fails?** Confirm that `.env` in the project root contains `ADMIN_USERNAME` and `ADMIN_PASSWORD`, then restart the app. Existing process environment values take precedence over `.env`. Sign in at `/admin/login` using those credentials.
