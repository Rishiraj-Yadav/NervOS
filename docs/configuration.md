# NervOS configuration

Milestone A2 provides one immutable API settings model backed only by process environment variables. NervOS does not automatically load `.env`, `.env.local`, or any other environment file. `.env.example` is documentation containing non-secret values only.

## Variables

| Variable | Default | Rules |
|---|---|---|
| `NERVOS_ENVIRONMENT` | `development` | One of `development`, `test`, or `production`. |
| `NERVOS_DATABASE_PATH` | `~/.nervos/nervos.db` | Must be non-blank and identify a file rather than an existing directory. User-home syntax is expanded and the path is resolved without creating it. |
| `NERVOS_APP_ORIGIN` | `http://localhost:5173` | One exact HTTP(S) origin with a host. Wildcards, credentials, paths, trailing slashes, queries, fragments, and invalid ports are rejected. Production requires HTTPS. |
| `NERVOS_LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

A relative database path is resolved from the process working directory. Repository commands run from the repository root; deployments should normally provide an absolute path.

Settings validation performs no filesystem mutation. The migration command creates the database parent directory immediately before connecting. Application construction and the health endpoint do not create the database.

Real environment files, credentials, API keys, and session secrets must remain local and untracked. A3 authentication adds no operator-configurable secrets. Cookie security is derived from the existing validated settings: `Secure` is enabled in production and whenever `NERVOS_APP_ORIGIN` uses HTTPS. The cookie name, seven-day lifetime, SameSite policy, and password/token policies are fixed application security constants documented in `docs/authentication.md`.
