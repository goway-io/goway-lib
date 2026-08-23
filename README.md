# goway-lib

Shared library for GoWay Python services. Owns the cross-service contracts:
base models and repositories (soft delete by default), RFC 9457 problem errors,
cursor pagination, idempotency, the transactional outbox over NATS JetStream,
auth dependencies, settings, and telemetry. No domain knowledge lives here.

```bash
make lint
make test
```
