"""Fingerprint the full data and diagnostic policy for reproducible checks."""
from hashlib import sha256
from pathlib import Path

def rule_source_version():
    root = Path(__file__).resolve().parents[1]
    digest = sha256()
    for name in ('config/diagnostic_rules.yaml', 'config/filter_mapping.yaml',
                 'bi_check_agent/core.py', 'bi_check_agent/service.py', 'bi_check_agent/db.py'):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()
