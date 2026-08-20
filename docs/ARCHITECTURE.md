# Architecture and authority boundaries

```text
files -> read-only intake -> extraction -> candidate analysis
                                      |             |
                                      v             v
                                    BKI         local model
                                      \             /
                                       review package
                                             |
                                             v
                                  Sovereign authorization
                                             |
                                             v
                                  separately built effects
```

The workbench never interprets a model response, BKI pass, route match, or human
interest as an authority grant. Every effect must name an exact operation and
target, obtain a fresh Sovereign grant, consume it atomically, and retain the
signed execution receipt for replay. Until the stable authorizer boundary is
available, effectful operations fail closed.

External recipients are data-disclosure boundaries. A future connector must
require recipient identity, approved content digest, disclosure classification,
channel, expiry, and a one-time capability. Approval for one recipient or
document cannot be reused for another.
