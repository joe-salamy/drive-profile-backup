---
name: optimize-repo-skills
description: Audits a repository's harness skills against the actual codebase, proposes conservative skill updates, and after explicit approval updates, creates, or removes repo-specific skills.
---

# Optimize Repo Skills

Use this skill when a repository's skill set needs periodic curation instead of routine load tracking. The goal is a small, high-signal set of repo-specific skills that reduce future agent mistakes without adding routine cognitive overhead.

## Required Input

- Repository root to analyze.
- Harness skills directory, usually `.<harness>/skills` such as `.omp/skills`.
- Any user preference about skill count, protected skills, or areas to ignore.

If the repo root or skills directory is ambiguous, inspect the repository layout first. Ask only when tools cannot resolve the ambiguity.

## Safety Rules

- Do not read, write, or diff `scratchpad.md`.
- Preserve user changes you did not make.
- Treat skill deletion and broad rewrites as destructive: propose them first and get explicit approval before applying them.
- Do not create generic skills. Every skill must map to observed repo conventions, recurring workflows, risk areas, or project-specific tooling.
- Prefer editing existing skills over replacing them when the skill's purpose is still valid.
- Keep final active repo skills lean. A typical repo should have 5-10 high-leverage skills unless the codebase clearly needs more.

## Workflow

1. Inventory the repo:
   - read existing repo instructions and README/developer docs;
   - inspect manifests, source layout, tests, migrations, deployment files, and scripts;
   - identify primary languages, frameworks, data stores, auth/security boundaries, build/test commands, and recurring workflows.

2. Inventory existing skills:
   - read every `SKILL.md` under the repo skills directory;
   - note each skill's trigger, concrete repo knowledge, commands, protected invariants, and overlap with other skills;
   - mark stale skills whose files, commands, frameworks, or workflows no longer exist.

3. Before creating new skills, research skill-authoring guidance:
   - search existing available skills for names/descriptions related to skill creation, skill writing, or skill design, then read the relevant `SKILL.md` files;
   - consult official harness skill documentation when available;
   - use web search for current external framework/library facts needed by any new or heavily updated skill, preferring official docs and source repositories.

4. Produce a curation proposal before destructive changes:
   - skills to keep unchanged, with reason;
   - skills to update, with exact gaps to fix;
   - skills to delete, with evidence that they are stale or redundant;
   - skills to create, with trigger condition, source evidence, and expected future failure avoided.

5. Ask for approval before applying deletes or broad rewrites. Small typo fixes or clearly non-destructive clarifications may be applied directly if they preserve behavior.

6. Apply approved changes:
   - one directory per skill: `<skills-root>/<skill-name>/SKILL.md`;
   - frontmatter must include exact `name` and a specific `description` explaining when to use it;
   - guidance must be concise and repo-specific: files, commands, invariants, edge cases, and verification checks that were confirmed during analysis;
   - remove obsolete skills completely after approval, including empty directories.

7. Verify the resulting skill set:
   - every skill has valid frontmatter;
   - descriptions have clear trigger conditions;
   - no two skills own the same job without a deliberate split;
   - commands and file paths referenced by skills exist or are explicitly documented as external prerequisites;
   - the final count and coverage match the repo's actual risk profile.

## Output Standard

Report:

- repo areas inspected;
- skills kept, changed, created, and deleted;
- web sources used for new or materially updated skills;
- approval decisions received before destructive changes;
- verification performed;
- residual risks or skills intentionally left unchanged.
