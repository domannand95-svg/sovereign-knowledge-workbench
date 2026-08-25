# Companion security and threat model

## Protected assets

- source bytes and SHA-256 identities;
- Sovereign signing key and authorization receipts;
- one-time grant and nonce ledger;
- staging manifests, rollback records and audit events.

## Trust boundaries

The browser and model output are untrusted. The loopback companion is a policy adapter, not an authority. Only the independently verified Sovereign receipt can cross the execution boundary. The source directory is never a staging target.

## Controls

- loopback-only bind, exact origin allowlist and bearer authentication;
- eight-hour token lifetime and five-minute plan lifetime by default;
- enforced API contract header and strict request-field validation;
- per-client request throttling and one-at-a-time plan claims;
- signed receipt verification plus atomic grant/nonce replay prevention;
- pre/post-copy hashes, crash recovery, rollback manifests and audit events;
- CI on Windows/Linux, dependency audit, CodeQL and SBOM workflow.

## Residual risks

A compromised local user account can read user-owned files and process memory. The companion is therefore not a multi-user security boundary. Tokens must not be pasted into tickets, commits or cloud logs. The local authorizer and signing key require separate operational protection.

## Dependency policy

Runtime dependencies must be narrowly pinned, reviewed through Dependabot, and pass dependency audit and CodeQL. New network-facing or cryptographic packages require explicit review. Generated SBOMs are release evidence, not authority.
