# Report Template

Use this template when producing the final publish-polish readiness report.

---

## Severity Definitions

| Severity       | Definition                                                                  |
| -------------- | --------------------------------------------------------------------------- |
| Blocker        | Prevents safe publication. Must fix before making repo public.              |
| Recommendation | Significantly improves quality or first impressions. Should fix.            |
| Optional       | Polish for mature open-source projects. Nice to have.                       |

## Verdict Decision Matrix

| Condition                                           | Verdict        |
| --------------------------------------------------- | -------------- |
| Zero blockers AND two or fewer recommendations      | **Ready**      |
| Zero blockers AND three or more recommendations     | **Almost Ready** |
| Any blockers                                        | **Needs Work** |

---

## Report Structure

```markdown
# Publication Readiness Report

**Project:** [name]
**Description:** [one-line description]
**Python version:** [version or constraint]
**Date:** [audit date]

## Verdict: [Ready / Almost Ready / Needs Work]

[One sentence summary explaining the verdict.]

---

## Blockers

[Items that MUST be fixed before publishing. If none, write "No blockers found."]

- **[Check name]** — [Description of the issue and what to do about it]
- **[Check name]** — [Description]

## Recommendations

[Items that SHOULD be fixed for a quality release. If none, write "No recommendations."]

- **[Check name]** — [Description of the issue and suggested fix]
- **[Check name]** — [Description]

## Optional Enhancements

[Nice-to-have items for mature projects. If none, write "No optional items to suggest."]

- **[Item]** — [Why it would help]
- **[Item]** — [Why it would help]

---

## Checklist Summary

| #  | Category             | Check                        | Status | Notes               |
| -- | -------------------- | ---------------------------- | ------ | ------------------- |
| 1  | Security & Privacy   | .gitignore completeness      | pass   |                     |
| 2  | Security & Privacy   | No tracked secret files      | pass   |                     |
| 3  | Security & Privacy   | No secrets in source         | pass   |                     |
| 4  | Security & Privacy   | No secrets in git history    | pass   |                     |
| 5  | Security & Privacy   | No hardcoded personal paths  | pass   |                     |
| 6  | Security & Privacy   | No personal information      | pass   |                     |
| 7  | License              | LICENSE file exists           | pass   |                     |
| 8  | License              | License in package metadata  | skip   | No pyproject.toml   |
| 9  | Code Quality         | Formatting & linting         | pass   |                     |
| 10 | Code Quality         | Docstrings on public API     | fail   | 3 functions missing |
| 11 | Code Quality         | Type hints on public API     | pass   |                     |
| 12 | Code Quality         | TODO/FIXME/HACK audit        | pass   |                     |
| 13 | Code Quality         | Tests exist and pass         | fail   | No tests found      |
| 14 | Code Quality         | No secrets in source         | pass   |                     |
| 15 | Documentation        | README exists                | pass   |                     |
| 16 | Documentation        | README required sections     | fail   | Missing: usage      |
| 17 | Documentation        | README freshness             | pass   |                     |
| 18 | Dependencies         | Dependency file exists       | pass   |                     |
| 19 | Dependencies         | Dependencies match imports   | pass   |                     |
| 20 | Dependencies         | Python version constraint    | fail   | Not declared        |
| 21 | Dependencies         | Dev deps separated           | skip   | Single requirements |
| 22 | Repo Hygiene         | No large binary files        | pass   |                     |
| 23 | Repo Hygiene         | No generated files committed | pass   |                     |

**Status values:** pass, fail, skip (not applicable), warn (needs manual review)
```

---

## Tone Guidelines

- Be factual and non-judgmental — the goal is to help the author ship
- Lead with what's working well before listing issues
- For each issue, include a concrete action ("add X to .gitignore", "run `git filter-repo`")
- Don't pile on — if many similar issues exist, group them ("12 public functions missing docstrings" rather than listing each one)
- Acknowledge that this is a personal project being shared — calibrate expectations accordingly
