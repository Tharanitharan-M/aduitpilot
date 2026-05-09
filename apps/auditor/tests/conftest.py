"""Test-suite-wide fixtures for the auditor service."""

from __future__ import annotations

import os

_DEFAULT_TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "development",
    "ADVERSARIAL_MODEL": "test:test",
    "AUDITOR_PUBLIC_URL": "http://localhost:8001",
    "LLM_BUDGET_CAP_USD": "0.50",
}

for _k, _v in _DEFAULT_TEST_ENV.items():
    os.environ.setdefault(_k, _v)
