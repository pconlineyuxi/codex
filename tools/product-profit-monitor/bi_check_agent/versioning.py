"""Separate executable provenance from the business identity of a check.

Increment POLICY_REVISION for changes to Python formulas, fixed SQL predicates,
NULL treatment, date/currency interpretation or finding-key construction. YAML
threshold, guard and filter changes are fingerprinted automatically. Formatting,
labels, aliases and infrastructure changes only affect the source/build version.
"""
from hashlib import sha256
import json
from pathlib import Path
import yaml

POLICY_REVISION = 1
# Verified equivalent builds, newest first. e903... differs only in display
# grouping of aggregate findings (service.summarize_findings), not evaluation.
# Older config-only fingerprints cannot prove SQL/formula identity and remain
# isolated. If multiple compatible generations exist, adopt the newest one.
LEGACY_SOURCE_VERSIONS = (
    '8e9a76a01a6975e57d82bff293134359cffbfd8a312f022b2cd4ccf727b4ac5a',
    'e903cd0a8a4d6709ff2bf89efec9f244b096b8d4790babf652c049b1393452b1',
)
LEGACY_POLICY_VERSION = '2561798ed75be8ad1e1f9b17ce0c17bceed8ffe5013c5c7d9b3b569d326fb291'


def diagnostic_policy_version():
    root = Path(__file__).resolve().parents[1]
    rules = yaml.safe_load((root / 'config/diagnostic_rules.yaml').read_text())
    filters = yaml.safe_load((root / 'config/filter_mapping.yaml').read_text())
    payload = {
        'revision': POLICY_REVISION,
        'rules': {key: {field: spec[field] for field in ('level', 'required_fields', 'tags')}
                  for key, spec in rules['queryable_rules'].items()},
        'thresholds': {key: value for key, value in rules['thresholds'].items()
                       if key in rules['queryable_rules']},
        'guards': {key: {field: value for field, value in spec.items() if field != 'guard_note'}
                   for key, spec in rules.get('rule_guards', {}).items()},
        'filters': {key: {field: value for field, value in spec.items()
                          if field in ('sql_field', 'operator', 'type')}
                    for key, spec in filters['filters'].items()},
    }
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compatible_legacy_versions(policy_version):
    return LEGACY_SOURCE_VERSIONS if policy_version == LEGACY_POLICY_VERSION else ()


def rule_source_version():
    """Full build fingerprint for audit, cache invalidation and in-flight fencing.

Keep the historical function name and evidence field for existing consumers.
This value no longer defines incident identity.
"""
    root = Path(__file__).resolve().parents[1]
    digest = sha256()
    paths = sorted([*root.glob('config/*.yaml'), *root.glob('bi_check_agent/*.py')])
    for path in paths:
        name = path.relative_to(root).as_posix()
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
