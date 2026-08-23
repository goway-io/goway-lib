"""Shared library for GoWay Python services.

Owns the cross-service contracts: base models and repositories, problem+json
errors, cursor pagination, idempotency, the transactional outbox, auth
dependencies, settings, and telemetry. No domain knowledge lives here.
"""
