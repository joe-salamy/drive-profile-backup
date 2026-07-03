---
name: merge-conflict-resolver
description: Resolves merge conflicts inside a temporary integration worktree created by the workflow script, preserves approved plan intent and latest base behavior, and writes a conflict-resolution summary without committing.
---

# Merge Conflict Resolver

Use this skill only inside a temporary integration worktree after the workflow script attempted to merge a completed feature branch and Git reported conflicts.

## Required Input

- `.<harness>/handoff/merge-conflict-context.md`
- The approved plan file
- `.<harness>/handoff/implementation-summary.md`
- `.<harness>/handoff/audit-summary.md`

If conflict context is missing, inspect `git status --short` and conflicted files, then state that context coverage is limited.

## Safety Rules

- Resolve conflicts only in the current integration worktree.
- Do not create, switch, merge, delete, rebase, or clean up worktrees.
- Do not commit. The script commits after post-conflict audit.
- Do not push.
- Do not read, write, or diff `scratchpad.md`.
- Do not broaden scope beyond conflict resolution.

## Workflow

1. Inspect the conflict state:

```powershell
git status --short
git diff --name-only --diff-filter=U
```

2. Read the merge conflict context, approved plan, implementation summary, and audit summary.

3. Resolve conflicts with these priorities:
   - Preserve latest base behavior unless the approved plan explicitly supersedes it.
   - Preserve audited feature behavior when compatible with latest base.
   - Treat the approved plan as intent and implementation details as evidence.
   - Prefer the smallest conflict-only edit.
   - Do not refactor unrelated code.

4. Remove all conflict markers and verify none remain:

```powershell
git diff --check
```

5. Run focused tests or checks for the conflicted files when practical. If checks cannot be run, record why.

6. Write `.<harness>/handoff/conflict-resolution-summary.md` with:
   - Conflicted files
   - Resolution decisions
   - Behavior preserved from base
   - Behavior preserved from feature
   - Tests/checks run and results
   - Skipped checks and residual risks

7. Leave the worktree ready for the script and post-conflict audit. Do not commit.
