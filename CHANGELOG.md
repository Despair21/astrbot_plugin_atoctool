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
  rate limiting and per-target skip handling.

### Changed

- Reminder delivery no longer skips `qq_official` / `qq_official_webhook` targets
  by UMO prefix; every target is handed to `Context.send_message`. AstrBot 4.29.4
  declares `support_proactive_message=True` and implements proactive
  `send_by_session` for both adapters, which supersedes the outdated [CORE-103]
  "MUST NOT" rule (and the stale `context.send_message` docstring it came from);
  the runtime source wins.
- `metadata.yaml` now declares `qq_official` and `qq_official_webhook` in
  `support_platforms` (18 -> 20).
- Reminder state schema bumped from v1 to v2. On first load, targets previously
  persisted as `skipped/platform_unsupported` (by the removed prefix rule) are
  migrated back to `pending` so they are retried; other skipped targets
  (`grace_expired`, `activity_ended`, ...) are untouched. The migration is
  idempotent because the schema version is persisted.
- Flattened the command set to single-token names (`/活动列表`, `/活动今日`,
  `/活动详情`, `/活动同步`, `/活动状态`, `/活动订阅`, `/活动取消订阅`); the old
  spaced syntax (for example `/活动 列表`) is deprecated and now falls back to
  help. English aliases are now `activity_*`.
- Replaced the bundled `helloworld` template with the game activity plugin
  (`version: 0.1.0`, verified against AstrBot 4.29.4 stable).
- Dashboard times now omit the time zone by default; the new `view.show_timezone`
  option (default off) appends it to every formatted time.
- Reminder status `sent` is labeled 已提交 / Submitted with a muted note that it
  means "handed to the platform adapter", not delivered; sends at least 1 minute
  after the scheduled time carry a 补发 / Catch-up badge. Reminder job facts are
  expanded on demand from a 详情 / Details toggle in the activity cell instead of a
  nested `<details>` or an always-visible row; mobile cards use the same toggle.
- Renamed ambiguous dashboard labels: activities/sources 最近发现/最近成功 →
  同步时间, sources 诊断详情 → 详情 (en-US: Last seen/Last success → Synced at,
  Diagnostics → Details).

### Fixed

- Reminder detail rows are collapsed by default, so each job no longer renders an
  always-visible extra table row. Details DOM ids are index-based
  (`reminder-details-<n>`) and the job key stays in `data-reminder-key`, so keys
  containing `#` or `:` never become CSS selectors.
- New targets added to a fixed reminder session (`target_umos`) or to a game
  subscription are now appended to existing jobs even when the job is already
  `sent`; the job is reopened as `pending` and only the new target is attempted.
  Already-sent targets are never resent.
- Documented the QQ official group cold-start limitation: AstrBot's proactive
  group send depends on an in-process group scene cache, so a cold group may be
  silently skipped by the core while `Context.send_message` still returns `True`;
  a `sent` reminder target is not a platform delivery receipt. The plugin now logs
  one warning per QQ official group send attempt.
