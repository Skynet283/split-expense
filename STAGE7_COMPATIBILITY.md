# Stage 7: group archive

`Group.archived_at` and `Group.archived_by_user_id` are nullable archive
metadata. A null timestamp means active. Archive is logical only: it preserves
the group, memberships, expense records/revisions/snapshots, settlement
requests, and confirmed settlements.

The active index excludes archived groups. Authorized members may still open an
archived group and expense history, but owners cannot create, revise, or void
expenses once it is archived. The existing group-delete endpoint now archives
every group, including an empty group; there is no Stage-7 physical deletion or
restore route.

Accounting deliberately does not filter archived groups. Global balances,
pairwise obligations, and group balance reconstruction continue to use the same
active expense snapshots and confirmed settlements. Settlement requests are
global rather than group-linked, so pending requests remain stored and their
existing confirm/reject behavior is unchanged after archive.
