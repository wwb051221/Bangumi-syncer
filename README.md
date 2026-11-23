# fnos2bgm

> Sync **fnOS (飞牛影视, `trim.media`)** anime watch progress to **Bangumi**.
> Multi-user, read-only data source, dry-run by default, human-reviewable matching —
> in a single zero-dependency container.

[![CI](https://github.com/fnos2bgm/fnos2bgm/actions/workflows/ci.yml/badge.svg)](https://github.com/fnos2bgm/fnos2bgm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

中文文档：[README.zh-CN.md](README.zh-CN.md) · Design notes: [architecture](docs/architecture.md) ·
[matching](docs/mapping.md) · [fnOS schema](docs/schema.md)

## How it works

```
┌─────────────────────┐        ┌──────────────────┐        ┌─────────────────┐
│ fnOS trim.media     │  ro    │     fnos2bgm     │ HTTPS  │   api.bgm.tv    │
│ trimmedia.db ───────┼───────▶│ snapshot → match │───────▶│ episode marks / │
│ (root 700, closed)  │ VACUUM │ → idempotent push│ OAuth  │ collection state│
└─────────────────────┘  INTO  └──────────────────┘        └─────────────────┘
```

The fnOS player often leaves `watched = 0` even when you finished an episode, so the
position/runtime ratio is the primary "finished" signal (`sync.watch_ratio`, default 90%).

## Features

- **Anime only** — filters by library path (`source.include_paths`), so dramas, movies and
  live channels never reach the matcher.
- **Multi-user** — every fnOS account is synced separately and can target its own Bangumi account.
- **Never writes the server DB** — a consistent copy is taken with `VACUUM INTO` over a
  read-only connection; playback is not interrupted.
- **Dry-run by default** — `app.dry_run = true` only logs what it *would* push.
- **Idempotent** — an episode is never pushed twice; optionally verifies Bangumi's own marks first.
- **Reviewable** — low-confidence matches are queued instead of guessed (`fnos2bgm review`,
  `fnos2bgm map add`).
- **Merged seasons** — map local episodes 25–48 to a second Bangumi subject with `--from/--to`.
- **Schema-drift tolerant** — required columns are probed, and the SQL can be overridden in config.
- **Zero runtime dependencies** — stdlib only, ~55 MB image.

## Quick start

```bash
git clone https://github.com/fnos2bgm/fnos2bgm.git
cd fnos2bgm
cp config.example.toml config.toml
```

1. Create a Bangumi application at <https://bgm.tv/dev/apps> with redirect URI
   `http://127.0.0.1:8765/oauth/callback`.
2. Fill in `[[target.accounts]]` (`client_id`, `client_secret`) and `[[users]]` in `config.toml`.
3. Authorise (token is stored in `/data/state/state.db`, refreshed automatically):

   ```bash
   docker compose run --rm fnos2bgm oauth --account main --manual
   ```

4. Rehearse one run with `dry_run = true`:

   ```bash
   docker compose run --rm fnos2bgm run --once
   ```

5. Set `app.dry_run = false`, then `docker compose up -d`. Status page: `http://<nas>:8099/`.

### Without Docker

```bash
pip install .
fnos2bgm --config config.toml run --once
```

The process needs read access to `trimmedia.db` (root, or `source.mode = "snapshot"` with a
copy prepared elsewhere).

## Multi-user

```toml
[[target.accounts]]
name = "me"
client_id = "..."
client_secret = "${BGM_CLIENT_SECRET}"

[[users]]
fnos_user = "wwb051221"   # fnOS username or guid
account = "me"

[[users]]
fnos_user = "2708"
account = "kids"
watch_ratio = 0.95
```

```bash
fnos2bgm oauth --account me
fnos2bgm oauth --account kids
```

## Manual review and mappings

```bash
fnos2bgm review                          # shows without a confident match + top candidates
fnos2bgm search -k "茶啊二中"             # find a subject id
fnos2bgm map list
fnos2bgm map add --account main --series 'fnos:<guid>:s1' --subject 148295
fnos2bgm map add --account main --series 'fnos:<guid>:s1' --subject 486347 --from 25 --to 48
```

Mappings are scoped to `(account, series, episode range)`; a change clears the push history of
that range so the next run re-pushes it.

Matching is conservative on purpose: title similarity + year + episode count + season marker,
and a subject is only auto-mapped when it beats the runner-up by `match.min_margin`. Everything
else waits for a human — a wrong mapping writes wrong history, a missing one just waits.

## Commands

```
fnos2bgm run [--once]        # loop (container default) or one shot
fnos2bgm status              # recent runs, tokens, counters
fnos2bgm review [--json]     # unmatched shows with candidates
fnos2bgm map list|add|remove # season -> subject mappings
fnos2bgm search -k KEYWORD   # look up Bangumi subject ids
fnos2bgm oauth --account X   # authorise (--manual works over SSH)
fnos2bgm dump [--json]       # what the media server reports, no writes
```

Exit codes: `0` ok, `1` run failed, `2` config error, `3` re-authorisation needed.

## Security

- The trim.media database is mounted **read-only**; only a copy is written, to our own volume.
- The container runs as root because the database is `root:700`. Prefer `source.mode = "snapshot"`
  with a host cron if you want to run unprivileged.
- Tokens live in `/data/state/state.db`, never in the config file; the config can be mounted read-only.
- No telemetry. Outbound traffic is limited to `api.bgm.tv` / `bgm.tv`.
- The status page is read-only; every mutation goes through the CLI.

## Development

```bash
pip install -e ".[dev]"
python -m pytest        # 81 offline tests (synthetic fnOS DB + fake tracker)
ruff check src tests
```

Layout and extension points (adding Jellyfin/Emby/Plex sources) are documented in
[docs/architecture.md](docs/architecture.md).

## License

[MIT](LICENSE). Not affiliated with fnOS or Bangumi; please respect the Bangumi API terms —
the default rate limit is one request per second.
