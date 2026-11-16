# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Six built-in game activity sources with conditional HTTP caching, retry
  classification and SSRF-safe DNS pinning.
- Persistent per-game snapshots with stable activity ids, content hashes,
  rename detection, tombstones and cross-source merging.
- `/活动` command group with list, today, detail, status, admin sync,
  subscribe and unsubscribe subcommands.
- Seven dashboard Web APIs (`health`, `overview`, `games`, `activities`,
  `sources`, `sync`, `reminders`) guarded by the dashboard session.
- Single `views/activity` dashboard view (overview, activities, sources,
  reminders) with zh-CN and en-US locales.
- End-before reminder scheduler with per-target retry state, backfill window,
  rate limiting and `qq_official` skip handling.

### Changed

- Replaced the bundled `helloworld` template with the game activity plugin
  (`version: 0.1.0`, verified against AstrBot 4.29.4 stable).
