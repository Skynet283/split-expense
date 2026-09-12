# Stage 1 compatibility

`User` is the global identity and `GroupMembership` is the authorization and group-membership relationship.  The existing `Member`, `Expense`, and `ExpenseSplit` tables remain unchanged because expenses still reference `Member`.

For groups created in Stage 1, the application creates one legacy `Member` row per selected `User` solely so the existing expense and balance engine can continue to operate.  These rows are not used to authenticate a person, authorize group access, or define global identity.  A later, explicit expense migration can replace them safely.

The initialization migration is additive only: it adds `users.name`, creates `group_membership`, and adds owner memberships because an existing group's `owner_id` is an unambiguous user relationship.  It does not map old group-local members to users by name, does not change old login IDs, and does not modify financial records.  Pre-Stage-1 accounts can have a missing display name or a non-10-digit login ID; those accounts continue to work unchanged, while all new registrations must provide a name and an exact 10-digit string User ID.
