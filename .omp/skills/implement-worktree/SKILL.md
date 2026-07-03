---
name: implement-worktree
description: Implements an approved Markdown plan inside the current script-created git worktree, verifies the result, commits the implementation, and writes a handoff summary for audit.
---

# Implement Worktree

Use this skill inside a feature worktree that has already been created by the workflow script. The script owns branch creation, worktree creation, merge, and cleanup. This skill owns implementation behavior inside the current worktree.

## Required Input

- Path to the approved plan file.
- Current shell location must be the feature worktree, not the primary `main` checkout.

If the plan path is missing or the current checkout is `main`, stop before editing and report the problem.

## Safety Rules

- Do not create, switch, merge, delete, rebase, or clean up worktrees.
- Do not edit the primary/main checkout.
- Do not push.
- Do not read, write, or diff `scratchpad.md`.
- Preserve user changes you did not make.
- Keep edits scoped to the approved plan.

## Workflow

1. Confirm the current branch and worktree:

```powershell
git worktree list
git branch --show-current
git status --short
```

Stop before editing if the current branch is `main` or if the current path is the primary `main` checkout.

2. Read the plan completely. Restate the implementation scope, non-goals, assumptions, and expected verification.

3. Implement the plan in the current worktree only. Follow this repository's `AGENTS.md` and any relevant skills.

4. Run focused tests or checks appropriate to the changed files. If a relevant check cannot be run, record the reason clearly.

5. Commit all intended implementation changes:

```powershell
git status --short
git add <changed-files>
git commit -m "Implement plan"
git status --short
```

Do not include `scratchpad.md` or workflow handoff artifacts. The workflow merge only receives committed branch changes; uncommitted or unstaged files left in the worktree will not be included in the squash/no-ff integration merge.

6. Write `.<harness>/handoff/implementation-summary.md` with:
   - Plan path
   - Worktree path
   - Branch name
   - Commit SHA, or a clear note if no commit was created
   - Changed files
   - Behavior changes
   - Tests/checks run and results
   - Skipped checks and why
   - Implementation decisions and tradeoffs
   - Assumptions, blockers, residual risks, or follow-up work

## Output Standard

The summary must be detailed enough for a context-cleared audit agent to verify the work without relying on chat history. Treat the summary as a handoff artifact, not a final response substitute.
