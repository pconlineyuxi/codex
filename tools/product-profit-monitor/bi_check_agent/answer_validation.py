"""Reject uncited numbers and unsupported causal assertions, independently of prompts."""
import math
import re

CITATION = re.compile(r'\[((?:source-|component-|diagnosis-)[^\]\s]+|total)\]')
NUMBER = re.compile(r'(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?%?')
CAUSAL = re.compile(r'根因|原因是|原因在于|因为|由于|导致|任务.{0,16}(?:失败|成功)|写入失败|刷新失败')
QUALIFIER = re.compile(r'可能|待.{0,5}(?:验证|核实|确认|核查)|尚未|不能|无法|不代表|不足以|未确认|未验证|不应|假设|算术|缺少|尚缺|缺乏|是否|并非|不是|不意味着|需(?:要|核对|核查|确认)|请.{0,4}(?:提供|核对|核查)')


def numbers(value):
    if isinstance(value,bool) or value is None: return []
    if isinstance(value,(int,float)):
        return [float(value)] if math.isfinite(value) else []
    if isinstance(value,str):
        return [float(m.group().rstrip('%').replace(',','')) for m in NUMBER.finditer(value)]
    if isinstance(value,dict):
        return [n for k,v in value.items() if k not in {'evidence_id','order_line_id','object_keys'} for n in numbers(v)]
    if isinstance(value,list): return [n for v in value for n in numbers(v)]
    return []


def validate_claims(content,evidence):
    items={item['evidence_id']:item for item in evidence['source_analyses'] if 'periods' in item or 'assessments' in item}
    for tool_result in evidence['source_analyses']:
        for report in tool_result.get('assessments',{}).values():
            if report:
                items.update({a['evidence_id']:a for a in report.get('assessments',[])})
    items.update({c['evidence_id']:c for c in evidence.get('components',[])})
    if evidence.get('totals'):items['total']=evidence['totals']
    citations=set(CITATION.findall(content))
    # The source population must be analyzed; a diagnosis reference is additional evidence.
    if not any(key in citations and 'periods' in item for key,item in items.items()) or citations-set(items):
        raise ValueError('missing or invalid evidence citation')
    allowed=numbers([items[key] for key in citations])
    allowed.extend(numbers(evidence.get('periods',{})))
    text=CITATION.sub('',content)
    text=re.sub(r'(?m)^\s*(?:\*\*)?\d+[.)、）]\s*','',text)
    for token in NUMBER.findall(text):
        value=float(token.rstrip('%').replace(',',''))
        candidates=[value,value/100] if token.endswith('%') else [value]
        if not any(abs(v-n)<=0.00500001+abs(n)*1e-8 for v in candidates for n in allowed):
            raise ValueError('answer contains unsupported numeric claim; use returned tool values')
    for sentence in re.split(r'[。！？；\n]',text):
        if CAUSAL.search(sentence) and not QUALIFIER.search(sentence):
            raise ValueError('causal conclusion needs explicit verification status; do not assert an unverified root cause')
