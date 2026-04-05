# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Open-source governance files: `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and CI workflow.
- Basic test coverage for dashboard password policy via `tests/test_auth_policy.py`.

### Changed

- Dashboard password minimum increased to 10 characters across shell setup and web signup.
- Signup validation now uses centralized policy in `auth_policy.py`.
- README corrected broken references and now documents repository layout and contribution/security docs.

### Removed

- Tracked internal planning artifact under `.kilo/`.
