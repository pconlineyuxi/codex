"""One metadata catalog for parsing, checking, UI and patrol reports.

Thresholds and guards live alongside these definitions in diagnostic_rules.yaml.
Detection formulas remain explicit Python in core.py; missing cost deliberately
has no required_fields because a missing value is itself the condition to detect.
"""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml


@dataclass(frozen=True)
class RuleDefinition:
    id: str
    label: str
    label_en: str
    description: str
    severity: str
    level: Literal['line', 'aggregate']
    aliases: tuple[str, ...]
    required_fields: tuple[str, ...]
    tags: tuple[str, ...]


def load_catalog():
    path = Path(__file__).resolve().parents[1] / 'config/diagnostic_rules.yaml'
    config = yaml.safe_load(path.read_text(encoding='utf-8'))
    rules = {}
    for key, spec in config['queryable_rules'].items():
        if spec['level'] not in ('line', 'aggregate'):
            raise ValueError(f'Invalid rule level: {key}')
        rules[key] = RuleDefinition(
            id=key, label=spec['label'], label_en=spec['label_en'],
            description=spec['description'], severity=spec['severity'],
            level=spec['level'], aliases=tuple(spec['aliases']),
            required_fields=tuple(spec['required_fields']), tags=tuple(spec.get('tags', ())),
        )
    return MappingProxyType(rules)


RULES = load_catalog()
RULE_IDS = tuple(RULES)
# Compatibility export: existing pages and headless scripts keep their API.
RULE_LABELS = {key: rule.label for key, rule in RULES.items()}


def rules_with_tag(tag):
    return tuple(key for key, rule in RULES.items() if tag in rule.tags)


def ensure_catalog_current():
    # Validators/parser aliases are initialized from the same catalog at startup.
    # A changed definition needs a restart, never a mixture of old/new metadata.
    if load_catalog() != RULES:
        raise RuntimeError('异常规则目录已更新，请重启工具后再查询，以保证解析与检查使用同一份规则。')
