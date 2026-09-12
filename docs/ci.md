# Continuous integration and security scanning

Stage A ships two GitHub Actions workflows. Both are read-only, reference no
secrets, and invoke **repository-owned commands** — no check logic is
reimplemented in YAML. Everything they run is also runnable locally.

- `.github/workflows/ci.yml`
- `.github/workflows/security.yml`

## Triggers

| Workflow | Triggers |
| --- | --- |
| `ci.yml` | `pull_request` (all branches), `push` to `main`, `workflow_dispatch` |
| `security.yml` | `pull_request` (all branches), `push` to `main`, weekly schedule (Mondays 04:17 UTC), `workflow_dispatch` |

Both use `pull_request`, never `pull_request_target`, so fork pull requests run
with a read-only token and no secrets. `ci.yml` also declares a `concurrency`
group that cancels superseded runs on the same ref.

## Jobs

### `check` — repository checks

Fast, offline and deterministic. It installs no browser and spawns no services.

```
uv lock --check
uv run python scripts/bootstrap.py --skip-browser
uv run python scripts/check.py check
```

`--skip-browser` installs the frozen Python and pnpm dependency sets without
provisioning Chromium. The default bootstrap is unchanged and still provisions
Chromium, so local behaviour is unaffected.

### `e2e` — deterministic browser journey

```
uv lock --check
uv run python scripts/bootstrap.py
pnpm --dir apps/web exec playwright install-deps chromium
uv run python scripts/check.py e2e
```

The second Playwright command installs **Linux runner system libraries only**.
It is a CI-runner concern and is deliberately absent from local bootstrap; see
[Playwright provisioning](#playwright-provisioning).

## The `check` versus `e2e` split

`scripts/check.py` exposes six command groups:

| Group | Contents |
| --- | --- |
| `lint` | `ruff check .`, `ruff format --check .`, `pnpm lint` |
| `typecheck` | `pyright`, `pnpm typecheck` |
| `test` | `pytest`, `pnpm test` |
| `security` | `scripts/security_scan.py` |
| `e2e` | `scripts/e2e.py` — the Chromium journey |
| `check` | `lint` + `typecheck` + `test` + `security` |

`check` deliberately excludes `e2e`. The dividing line is principled: `check` is
everything fast, offline and deterministic; `e2e` is the one heavy journey that
needs a downloaded browser and spawns API, Vite and Chromium process trees.
Folding E2E into `check` would make routine lint/typecheck runs require Chromium
and make them environment-sensitive.

**Full verification is `check` then `e2e`**, both locally and in CI. The two CI
jobs run exactly those two commands.

## Tool and runtime versions

| Tool | Source of truth | CI value |
| --- | --- | --- |
| Python | `.python-version` | `uv python install` (reads the file) |
| Node.js | `.node-version` | `actions/setup-node` via `node-version-file` |
| pnpm | `package.json` → `packageManager` | `pnpm/action-setup` reads it; **no version is repeated in YAML** |
| uv | workflow pin + `MINIMUM_UV` in `scripts/bootstrap.py` | `0.9.15` |

**Node is deliberately 24 in CI while the supported range remains
`>=22.12.0 <25`.** `.node-version` records the CI value so a version manager can
align a developer machine in one step, but `bootstrap.py` continues to accept the
whole documented range — a developer on Node 22.12+ is not blocked. CI validates
the upper bound of the supported range; it does not narrow the local range.

**uv is the one version A6 introduced**, because the repository declared none.
CI pins `0.9.15` and `bootstrap.py` rejects anything older with a clear message.
The workflow pin and `MINIMUM_UV` must move together.

## Frozen dependencies

- `uv lock --check` runs as an explicit step — the lockfile is verified, never regenerated.
- `scripts/bootstrap.py` is the single authoritative installer; CI calls it rather than restating install commands.
- `UV_FROZEN=1` is set for the job so uv cannot mutate `uv.lock` as a side effect.
- `pnpm install` always runs with `--frozen-lockfile`.

If frozen validation fails in CI, that is a real finding. Fix the cause; do not
regenerate the lockfile to make it pass.

## Cache strategy

| Cached | Mechanism | Keyed on |
| --- | --- | --- |
| uv download cache | `setup-uv` `enable-cache` | `uv.lock` |
| pnpm store | `setup-node` `cache: pnpm` | `pnpm-lock.yaml` |

Only package-manager download caches are used. **Never cached:** `.venv/`,
`node_modules/`, `~/.nervos/`, any `*.db`/`*.sqlite*`, E2E temporary directories,
`playwright-report/`, `test-results/`, `blob-report/`, `.env*`. No database,
session, or user state can cross a job boundary.

Playwright browser binaries are intentionally **not** cached. A stale browser
cache is a common source of non-reproducible E2E failures, and the download is
short. Caching `~/.cache/ms-playwright` keyed to the locked `@playwright/test`
version is a possible future optimization.

## Permissions and security

Both workflows declare exactly:

```yaml
permissions:
  contents: read
```

No job or step escalates. No workflow references `${{ secrets... }}`, writes to
the repository, opens a pull request, publishes a package, or uploads an
artifact. No external AI provider, SaaS scanner, or third-party service is used.

### Action pinning policy

Every external action is pinned to a **verified full-length commit SHA** with the
human-readable release in a trailing comment. A moving major tag such as `@v7`
would let upstream change the code that runs in this repository without any
change here, so tags are not used.

| Action | Release | Commit SHA | Verified |
| --- | --- | --- | --- |
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | 2026-07-17 |
| `actions/setup-node` | v7.0.0 | `820762786026740c76f36085b0efc47a31fe5020` | 2026-07-14 |
| `actions/setup-python` | v7.0.0 | `5fda3b95a4ea91299a34e894583c3862153e4b97` | 2026-07-20 |
| `pnpm/action-setup` | v6.0.10 | `0977fd99725f1db4007ccb2928dbb4e90d06cc86` | 2026-08-03 |
| `astral-sh/setup-uv` | v10.1.0 | `bec219d24cd3e171d82865faccec33120bb574f4` | 2026-09-10 |

Each SHA was resolved from the official upstream repository
(`api.github.com/repos/<owner>/<repo>/commits/<sha>`). `tests/test_tooling.py`
enforces the policy: every `uses:` reference must match a 40-hex-character SHA
and carry a release label, and no workflow may grant write permission, reference
secrets, or use `pull_request_target`.

Updating an action means resolving a new SHA from the upstream repository and
updating the table above in the same change. Automated update tooling
(Dependabot/Renovate) is not configured; that is a later decision.

## Playwright provisioning

- Browser binaries come from the **project-local, locked** `@playwright/test`, via `scripts/bootstrap.py` → `pnpm --dir apps/web exec playwright install chromium`. There is no reliance on a system Chrome, a global Playwright, or `--with-deps` locally.
- `ci.yml` additionally runs `pnpm --dir apps/web exec playwright install-deps chromium` to install Linux runner system libraries. This touches OS packages on an ephemeral runner and never on a developer machine.
- The A5 contract is preserved unchanged: one Chromium project, one worker, zero retries, a freshly migrated temporary SQLite database per invocation, dynamic loopback ports, bounded readiness checks, and no arbitrary sleeps.
- **Flakiness is never hidden with retries.** If the journey proves flaky, make it deterministic instead of raising `retries`.

## Security scanning

`scripts/security_scan.py` detects accidentally tracked sensitive or
forbidden artifacts. It is standard-library only, so `security.yml` installs no
project dependencies.

### Scope

The scan is scoped by Git, never by a filesystem walk:

- **Local (default):** `git ls-files --cached --others --exclude-standard` — tracked files plus untracked files that are *not* ignored. This catches a problem before it is ever staged.
- **CI (`--tracked-only`):** `git ls-files --cached` — already-tracked files only.

Ignored paths such as `node_modules/`, `.venv/` and `.serena/` are never
enumerated. The scanner never walks an arbitrary directory.

### Rules

**Path rules** are structural and applied to every candidate path.

| Rule | Matches |
| --- | --- |
| `FORBIDDEN_ENV` | `.env`, `.env.*` except `.env.example` |
| `FORBIDDEN_SECRETS_DIR` | any path segment `secrets` or `.ssh` |
| `FORBIDDEN_KEY_FILE` | `id_rsa`, `id_ed25519`, `*.pem`, `*.key`, `*.p12`, `*.pfx` |
| `FORBIDDEN_DATABASE` | `*.db`, `*.db-shm`, `*.db-wal`, `*.sqlite`, `*.sqlite3`, `.nervos/` |
| `FORBIDDEN_LOG` | `*.log` |
| `FORBIDDEN_REPORT` | `playwright-report/`, `test-results/`, `blob-report/`, `htmlcov/`, `coverage/`, `.coverage*` |
| `FORBIDDEN_VENDOR` | `node_modules/`, `.venv/`, `__pycache__/`, `*.pyc` |
| `FORBIDDEN_LOCAL_STATE` | `.mcp.json`, `.claude/settings.local.json`, `.vscode/`, `.idea/`, `.serena/`, OS metadata files |

**Content rules** are anchored to provider-specific shapes and applied to text
files of at most 1 MiB: `PRIVATE_KEY_BLOCK`, `ANTHROPIC_KEY`, `OPENAI_STYLE_KEY`,
`GITHUB_TOKEN`, `AWS_ACCESS_KEY_ID`, `SLACK_TOKEN`, `GOOGLE_API_KEY`. A file whose
first bytes are the SQLite magic header is reported as `SQLITE_FILE_HEADER` even
if it is binary.

### Reporting

A finding reports **only** a path, an optional line number, a rule identifier, a
category, and a safe explanation:

```
apps/api/src/example.py:12: AWS_ACCESS_KEY_ID [credential] AWS access key id
```

The scanner never prints a matched value, the matched line, or surrounding
context. `tests/test_security_scan.py` asserts this directly: a sentinel value is
placed on the matching line and the test fails if it appears anywhere in stdout
or stderr.

Exit codes: `0` clean, `1` findings, `2` usage or internal error.

### Exceptions and false-positive control

- Structural path rules cannot false-positive and **cannot be suppressed**.
- Content rules use anchored provider prefixes with fixed lengths. No entropy-only scanning and no bare `password = …` matching.
- Generic-shaped matches containing an obvious placeholder (`example`, `changeme`, `dummy`, `fake`, `redacted`, `<…>`, `xxxx`, …) are suppressed.
- Files larger than 1 MiB and binary files skip content rules; path rules still apply.
- A line (or the line directly above) may carry `security-scan: allow RULE-ID` with a short justification. This suppresses only content rules and only the named rule id.

`graphify-out/` is **not** exempt. Its tracked report files are derived from
source text, so content scanning still applies to them; only the generated
`graphify-out/cache/` tree is ignored, because it is local state that is rebuilt
on demand.

## Clean-environment verification

`scripts/clean_check.py` proves the current working tree is self-contained. It:

1. enumerates the file set Git considers part of the repository;
2. copies it into a fresh temporary directory;
3. initializes an isolated Git repository there and runs `git add -A`;
4. runs `uv lock --check`, `bootstrap.py`, `check`, `pnpm build`, and `e2e`;
5. deletes the temporary directory.

No commit is created, so no Git author identity is required. The active checkout
is only ever read — the export is a copy, and `git status`/`git rev-parse HEAD`
are unchanged by a run. This is what allows an uncommitted working tree to be
verified as if it were a clean checkout. `tests/test_clean_check.py` asserts both
properties.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Required command 'uv' was not found` | Install uv outside the repository, then rerun bootstrap. |
| `NervOS requires uv 0.9.15 or newer` | Upgrade uv; the CI pin and `MINIMUM_UV` must move together. |
| Chromium missing | `pnpm --dir apps/web exec playwright install chromium`. |
| E2E fails only on the runner | Confirm the `install-deps chromium` step ran; it installs Linux system libraries. |
| `Vite readiness failed` / API early exit | Port handoff or startup failure. Read the supervisor's bounded log tail; do not substitute `localhost` for the `127.0.0.1` test Origin. |
| `NERVOS_E2E_WEB_ORIGIN is required` | Playwright was started directly. Run E2E through `scripts/check.py e2e`, which supplies the origin. |
| Security scan reports a file | The finding names a path and rule only. Remove the artifact or add a narrow, justified `security-scan: allow RULE-ID` for a content rule. |
| Frozen dependency failure | A lockfile is out of step. Fix the cause; do not regenerate to silence it. |
