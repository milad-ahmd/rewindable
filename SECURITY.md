# Security policy

Please **do not** open a public issue for security problems. Report them through GitHub's [private vulnerability reporting](https://github.com/milad-ahmd/rewindable/security/advisories/new) instead. You should hear back within a week.

Things worth knowing when you deploy rewindable:

- The journal stores model inputs and outputs, tool arguments and results **in plain text**. Treat `.rewindable/runs.db` like any other database that may hold sensitive data, and keep it out of version control (it is in `.gitignore` by default).
- `rewindable resume` imports and runs the workflow recorded in the journal. Only resume journals you trust.
- Tools marked `requires_approval=True` are only as safe as the people who can write to the journal.
