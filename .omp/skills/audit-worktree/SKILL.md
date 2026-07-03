---
name: audit-worktree
description: Audits an implementation worktree branch against main using a provided implementation summary, loads relevant project skills as needed, fixes all identified issues in the current worktree only, verifies the result, and reports the audit outcome. Use after script-driven worktree implementation when <harness> context has been cleared and the user passes the prior implementation summary.
---

# Audit Worktree

Use this skill inside an implementation worktree after another <harness> session has completed a plan and returned a full implementation summary. The goal is to audit the current branch against `main`, use the provided summary as intent/context, load relevant project skills as necessary, identify issues, fix them in this worktree, and verify the final state.

## Required Input

- The implementation summary from the prior script-driven worktree run.
- The current shell location should be the implementation worktree, not the primary `main` checkout.

If the summary is missing, continue with the git diff but state that intent coverage is limited. If the current checkout is `main`, stop before editing and tell the user to run the skill from the implementation worktree.

## Safety Rules

- Do not edit the primary/main worktree.
- Do not switch to or edit `main`.
- Do not merge, rebase, reset, or force-push unless the user explicitly asks.
- Do not read, write, or diff `scratchpad.md`.
- Preserve user changes you did not make.
- Make all fixes only in the current implementation worktree.

## Workflow

1. Confirm the current worktree and branch:

```powershell
git worktree list
git branch --show-current
git status --short
```

Stop before editing if the current branch is `main` or if `git worktree list` shows the current path is the primary `main` checkout.

2. Establish the comparison base:

```powershell
git fetch --all --prune
git merge-base main HEAD
git diff --stat main...HEAD -- . ":(exclude)scratchpad.md" ":(exclude)docs/scratchpad.md"
git diff --name-only main...HEAD -- . ":(exclude)scratchpad.md" ":(exclude)docs/scratchpad.md"
```

If local `main` is unavailable, inspect branches and use the repository default branch. Do not check out that branch.

3. Read the provided implementation summary and map it to the changed files. Treat the summary as intent, not proof. Verify the actual diff.

4. Audit the full diff against `main`:
   - Check correctness, regressions, missing edge cases, missing tests, and incomplete plan items.
   - Check this repo's architecture rules from `AGENTS.md`, especially CLI/web behavioral alignment, `EssayPipeline.run_step()`, index-to-text replacement, `_metadata.input_messages`, Supabase RLS/storage rules, prompt/schema pairing, and callback/usage tracking for LLM calls.
   - Check that new behavior is represented in focused tests or explain why tests are not feasible.
   - Inspect relevant existing code paths, not only changed lines, when needed to validate behavior.

5. Load project skills as necessary based on the changed files and risk areas. Read the relevant `.agent-harness/skills/<skill-name>/SKILL.md` files directly, then follow their audit guidance. Common mappings:
   - FastAPI routes, middleware, ARQ worker APIs: `api-review`
   - SSE or HITL streaming: `sse-audit`
   - Supabase migrations, RLS, storage, auth, SQL: `supabase`, `supabase-schema`, `supabase-postgres-best-practices`
   - Python async, typing, pipeline code: `python-pro`
   - Next.js, React, TypeScript, shadcn, frontend hooks: `ts-pro`, `audit-ts`, `vercel-react-best-practices`, `nextjs-shadcn`
   - Pydantic schemas or generated frontend types: `cross-stack-types`
   - Prompt templates or structured-output schemas: `prompt-schema-conventions`
   - Docker or deployment files: `docker-review`
   - Broad code quality/security review: `code-reviewer`

6. Create a concise issue list before editing. Prioritize bugs, security issues, data loss, broken contracts, missing migrations/RLS, missing callback/usage tracking, CLI/web drift, and test gaps.

7. Fix all confirmed issues in the current worktree. Keep edits scoped to the audit findings. Do not make unrelated refactors.

8. Re-run focused verification appropriate to the final diff. If the implementation worktree does not have its own `venv`, run Python/test commands from the worktree but activate the original checkout's virtualenv path:

```powershell
& <primary-checkout>\venv\Scripts\Activate.ps1
```

Keep all file operations in the implementation worktree.

For frontend changes, prefer the existing package scripts from `frontend/package.json`. If generated frontend types are affected, run the documented generation flow only when the API server requirement can be satisfied; otherwise report the skipped generation clearly.

9. Commit all intended audit fixes on the current branch if changes were made:

```powershell
git status --short
git add <fixed-files>
git commit -m "Fix audit findings"
git status --short
```

Do not include `scratchpad.md` or workflow handoff artifacts. If no fixes were needed, do not create an empty commit. The workflow merge only receives committed branch changes; uncommitted or unstaged files left in the worktree will not be included in the squash/no-ff integration merge.

10. Final response must include:

- Worktree path
- Branch name
- Base branch/ref used for diff
- Summary received from the prior implementation, briefly restated
- Skills loaded and why
- Issues found and fixes applied
- Files changed by the audit
- Commit SHA for audit fixes, or note that no commit was created
- Tests/checks run and any skipped checks
- Residual risks or follow-up work

## Output Standard

Be explicit about both findings and fixes. If an issue is suspected but not confirmed, label it as a residual risk instead of silently editing around it. The final report should let a reviewer understand what changed in the audit pass without reopening the entire diff.
