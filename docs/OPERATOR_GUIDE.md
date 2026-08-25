# Solar Forge operator guide

## Boundary

The browser is a projection and never an authority. The companion listens only on `127.0.0.1`, requires a temporary token and exact contract version, and accepts no browser-supplied receipt. Planning is inert. Staging and rollback each invoke the local Sovereign authorizer independently.

## Seven stages

1. **Ingest:** hash local bytes without modifying them.
2. **Extract:** produce attributable, non-authoritative observations.
3. **Propose:** package proposed changes as intent artifacts.
4. **Pre-flight:** create a deterministic, expiring staging plan.
5. **Human review:** expose conflicts, citations, target and rollback details.
6. **Authorize:** verify a short-lived signed receipt and consume its grant and nonce once.
7. **Ledger:** record the result and retain a bounded rollback manifest.

## Companion lifecycle

```powershell
.\scripts\start-companion.ps1 -StateDb .\workbench-output\state.db `
  -StagingDb .\workbench-output\staging.db
.\scripts\companion-status.ps1
.\scripts\stop-companion.ps1
```

The startup window prints the temporary token required by the Base44 screen. The session file is permission-restricted where supported, removed on orderly shutdown, and expires after eight hours by default.

## Recovery

An interrupted copy never becomes authorized merely because bytes exist. Run the existing `stage-recover` command, inspect the result, and require a new plan and receipt for any retry. Never manually edit staging database records.

## Synthetic-only readiness testing

Use `demo/synthetic/source-record.txt`. Do not point development builds at the Digital Archive or its 4,084 candidates. Confirm the source remains byte-for-byte identical after staging and rollback.
