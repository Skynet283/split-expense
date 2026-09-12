# Stage 8: explicit admin role

`users.is_admin` is a non-null boolean that defaults to false. The additive
startup upgrade adds it with `DEFAULT 0` and updates any null legacy values to
false. Public registration does not accept this field.

There is no bootstrap promotion, magic login ID, special password, or public
promotion route. A trusted operator assigns `is_admin` directly in the database.
The existing configured initial account is not automatically an administrator.

`/admin` is read-only and uses `admin_required`: anonymous users follow the
normal login behavior and authenticated non-admin users receive 403. It shows
safe operational data only: user names/login IDs/admin flag, group owner/archive
state/member and history counts, global balances, and unresolved active-expense
count. Password hashes and mutation controls are never displayed.

Admin status does not alter group ownership, archive policy, settlement rules,
or immutable financial-history paths.
