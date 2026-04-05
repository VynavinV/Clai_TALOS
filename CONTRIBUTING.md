# Contributing to Clai TALOS

Thanks for helping improve TALOS.

## Quick Start

1. Fork and clone the repo.
2. Run `./start.sh` (macOS/Linux) or `start.bat` (Windows).
3. Make your changes in a focused branch.
4. Run checks before opening a PR:
   - `python -m compileall -q .`
   - `pytest -q tests`

## Contribution Guidelines

- Keep changes small and focused.
- Prefer explicit errors over silent fallbacks.
- Preserve the single-process architecture unless there is a clear, measurable benefit.
- Avoid large dependency additions without justification.
- Add or update tests for behavior changes.
- Update docs when user-facing behavior changes.

## Pull Request Checklist

- [ ] Scope is focused and intentional.
- [ ] New behavior is documented in `README.md` or `tools/*.md`.
- [ ] Tests added/updated where applicable.
- [ ] No secrets, local DB files, or runtime artifacts included.

## Commit Style

Use clear commit messages that explain intent and impact.

Examples:

- `fix: validate dashboard password policy in signup API`
- `docs: add security and contribution guidance`
- `ci: add smoke checks and auth policy tests`
