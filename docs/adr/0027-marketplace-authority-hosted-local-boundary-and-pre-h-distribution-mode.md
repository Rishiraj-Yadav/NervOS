# ADR 0027 — Marketplace Authority, Hosted–Local Boundary, and Pre-H Distribution Mode

Status: Accepted for I0 architecture freeze / pending external acceptance

## Context

Stages A–G and the Gemini extension are merged. The operator selected bounded Marketplace
distribution before Stage-H hardening. Marketplace must not inherit local execution or trust
authority. The canonical contract is [Stage-I master plan](../stage-i/README.md).

## Decision

Stage I distributes completed locally built/signed `.nervos` releases. Local NervOS independently
verifies, explicitly authorizes and installs them through Stage G. The intentional sequence is
**G → bounded/pre-H I → H hardening**, without renaming H/I or declaring H complete.

| Authority | Owner |
| --- | --- |
| Claims, execution, retries and terminal state | C |
| Grants, live tool/MCP permission and audit | D |
| Automatic Run creation | E |
| Conversations, context and memory | F |
| Artifact protocol, signatures, installation, pinning and lifecycle | G |
| Sandbox, secrets, interactive approval, persistent trust and local revocation enforcement | H |
| Hosted publisher ownership, catalog, publication and distribution observations | I |

Future hosted `apps/marketplace` has independent authentication, PostgreSQL-class metadata,
private S3-compatible immutable objects and its own migrations. Local SQLite is unchanged;
hosted tables never enter the runtime database. I0 leaves the placeholder inactive.

V1 has one operator-configured origin: public HTTPS by default; explicit admin private/
self-hosted HTTPS permitted; loopback HTTP only in explicit development/test mode. Ordinary
requests cannot provide remote destinations. Reject URL credentials/query/fragment, fail
closed on redirects, validate destination policy at actual connection time and avoid unintended
proxy credential inheritance. Browser uses authenticated local API, never publisher credentials.
Concrete transport stays outside core; I3 selects placement rather than mandating a new package.

Marketplace receives only necessary discovery/download information, never local IDs by default,
configuration, Runs, memory, conversations, provider/tool secrets or grants. No default telemetry.
Worker, Scheduler, SDK and package-host have no Marketplace dependency. Outage may block remote
operations but not installed execution, local history/memory/tools or manual G installation.

Pre-H publication/static verification/signatures do not establish safe code or local trust.
Publisher identity, signer identity and local trust are distinct. I has no sandbox, encrypted
secret manager, interactive Ask/Approve/Deny, persistent publisher trust or automatic local
revocation enforcement. Typed observations are the future H seam, never direct local policy.

## Consequences and alternatives

Separate hosted infrastructure serves distribution without converting the local modular
monolith into microservices. Embedding publisher tables in local SQLite, giving Marketplace
runtime mutation authority, or equating verified accounts with trusted code is rejected.
Private HTTPS self-hosting remains supported. Hostile signed code remains a pre-H risk.

## Verification and governance

I1 proves hosted separation; I3 proves origin/privacy/transport and outage boundaries; I4/I5
prove sole G handoff and offline execution. Follow the master acceptance matrix. This ADR
implements governance only and awaits external acceptance; H and I1 are not started.
Conflicts require `STAGE I ARCHITECTURE CHANGE REQUEST — <issue>`.
