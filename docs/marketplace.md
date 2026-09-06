# NervOS Marketplace

## Status

Target architecture. Marketplace is not part of Stage A.

## Role

The marketplace distributes agent packages. It does not run the user's local AgentInstance.

```text
Developer -> Marketplace -> User's NervOS -> local AgentInstance
```

## Responsibilities

Future marketplace responsibilities:

- publisher identity
- package metadata
- versions
- package storage
- signatures/hashes
- compatibility metadata
- screenshots/docs
- search/discovery
- install counts/ratings later
- moderation/security status later

## What remains local

Normal user-specific state remains on the user's NervOS node:

- AgentInstance config
- sessions
- agent memory
- secrets
- tool credentials
- run logs/artifacts unless explicitly exported

## Developer flow

```text
developer builds agent
  -> nervos test
  -> nervos package
  -> sign package
  -> nervos publish
  -> marketplace validation
  -> listing/version available
```

## User flow

```text
browse listing
  -> choose version
  -> local NervOS downloads package
  -> local package manager verifies it
  -> local permission/configuration flow
  -> local AgentInstance created
  -> local runtime executes future jobs
```

Marketplace installs must reuse the same local package installer used for a manually supplied `.nervos` file.

## Hosted marketplace storage

A future hosted marketplace can use a relational DB for metadata and S3-compatible object storage for packages/assets. This is separate from the user's self-hosted runtime database.

## Trust

Possible future states include unverified/verified publisher, scan status, signature status, revoked version, and known-vulnerability warning. A marketplace listing is never proof that arbitrary code is safe; runtime sandboxing and permissions remain necessary.
