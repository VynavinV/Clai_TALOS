# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Open-source governance files: `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and CI workflow.
- Basic test coverage for dashboard password policy via `tests/test_auth_policy.py`.
- Initial Windows EXE packaging pipeline: `talos_entry.py`, `talos_exe.spec`, `scripts/build_windows_exe.ps1`, and `.github/workflows/windows-exe-release.yml`.
- Runtime path abstraction in `app_paths.py` for frozen executable mode (app data storage under OS user data directories).
- Over-the-air update support in Settings with authenticated update check/apply APIs and runtime update handling for source git installs and frozen Windows builds.
- OTA channel control (`stable` / `prerelease`) and release-notes preview in Settings.
- Legacy updater utility `scripts/update_legacy_copy.py` for non-git copied installs that predate OTA support.

### Changed

- Dashboard password minimum increased to 10 characters across shell setup and web signup.
- Signup validation now uses centralized policy in `auth_policy.py`.
- README corrected broken references and now documents repository layout and contribution/security docs.
- Core modules now resolve writable runtime files (env, db, logs, projects, oauth cache, tool configs) through centralized runtime paths for EXE-safe behavior.

### Removed

- Tracked internal planning artifact under `.kilo/`.
