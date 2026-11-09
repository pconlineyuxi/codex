"""Explain zero base cost from purchase/receipt evidence, without rewriting costs.

A completed lookup for one applicable purchase product can establish that zero
base cost is expected. Gift classification is a separate finding.
"""
from collections import defaultdict
from html import unescape
import re
import pandas as pd
from bi_check_agent.evidence_reader import Reader, bindings
from bi_check_agent.patrol_policy import POLICY, number, present

LABELS = {
    'verified_gift_receipt': '无 PO、采购价为零，已核实零成本赠品入库及出库数量',
    'verified_no_po_zero_cost': '已核实无适用 PO，原始基础成本为零',
    'purchase_cost_available': '查到非零采购价格来源，需要核对成本计算或带入链路',
    'po_requires_review': '存在采购记录，不能按无 PO 零成本排除',
    'source_missing_or_duplicate': '成本来源缺失或不唯一，保留疑点',
    'evidence_read_failed': '采购／入库证据读取未完成，保留疑点',
}


def plain_note(value):
    return re.sub(r'\s+', '', unescape(re.sub(r'<[^>]*>', '', str(value or ''))))


def can_check_gift(context):
    return (context.get('tracking_method') == 'upc' and context.get('tracking') == 'none'
            and context.get('is_distribution') is False and not present(context.get('main_upc'))
            and number(context.get('purchase_price')) == 0 and number(context.get('fallback_purchase_price')) == 0
            and number(context.get('fallback_matches')) == 1)


def incomplete(review):
    return review['status'] != 'reviewed' or review.get('receipt_check_status') == 'failed'


def judge(context, purchases, moves, messages):
    """Pure evidence decision. Private row identifiers never enter the result."""
    prices = [number(r.get('price_unit')) for r in purchases]
    report = {
        'status': 'reviewed', 'gift_verified': False,
        'actual_ir': str(context['actual_ir']) if present(context.get('actual_ir')) else None,
        'po_line_count': len(purchases),
        'po_numbers': sorted({str(r['po_number']) for r in purchases if present(r.get('po_number'))}),
        'po_scope': 'main_upc' if present(context.get('main_upc')) else 'product',
        'purchase_price': number(context.get('purchase_price')),
        'fallback_purchase_price': number(context.get('fallback_purchase_price')),
        'po_price_min': min((p for p in prices if p is not None), default=None),
        'po_price_max': max((p for p in prices if p is not None), default=None),
        'po_currencies': sorted({str(r['currency']) for r in purchases if present(r.get('currency'))}),
        'receipt_numbers': sorted({str(m['receipt']) for m in moves if m.get('source_usage') != 'internal'
                                   and m.get('dest_usage') == 'internal' and present(m.get('receipt'))}),
        'gift_receipts': [],
    }
    if purchases:
        price_sources = prices + [report['purchase_price'], report['fallback_purchase_price']]
        report['code'] = ('purchase_cost_available' if any(p is not None and p != 0 for p in price_sources)
                          else 'po_requires_review')
    elif number(context.get('raw_cost')) == 0:
        report['code'] = 'verified_no_po_zero_cost'
        if moves and can_check_gift(context):
            verified, receipts = gift_inventory(context, moves, messages)
            if verified:
                report.update(code='verified_gift_receipt', gift_verified=True, gift_receipts=receipts)
    else:
        report.update(status='incomplete', code='source_missing_or_duplicate')
    report['summary'] = LABELS[report['code']]
    report['basis'] = 'current_purchase_and_inventory_evidence'
    return report


def gift_inventory(context, moves, messages):
    if not moves or len({r.get('move_id') for r in moves}) != len(moves):
        return False, []
    approved = {plain_note(s) for s in POLICY['gift_receipt_notes']}
    noted = {m['picking_id'] for m in messages if plain_note(m.get('body')) in approved}
    inbound, outgoing = [], []
    balances = defaultdict(float)
    target = False
    for move in moves:
        qty = number(move.get('product_qty'))
        if qty is None or qty <= 0 or not present(move.get('move_date')) or move.get('state') != 'done':
            return False, []
        src, dst = move.get('source_usage'), move.get('dest_usage')
        if src == dst == 'internal':
            continue
        if any(present(move.get(k)) for k in ('inventory_id','production_id','origin_returned_move_id')):
            return False, []
        if src == 'supplier' and dst == 'internal':
            if (number(move.get('price_unit')) != 0 or present(move.get('purchase_line_id'))
                    or present(move.get('created_purchase_line_id')) or present(move.get('procurement_id'))
                    or move.get('picking_id') not in noted or move.get('receipt_state') != 'done'):
                return False, []
            inbound.append(move)
            balances[str(move['move_date'])] += qty
        elif src == 'internal' and dst == 'customer':
            outgoing.append(move)
            balances[str(move['move_date'])] -= qty
            target |= move.get('sale_line_id') == context.get('order_line_id')
        else:
            # Returns, adjustments, transit, manufacturing and unknown sources
            # need their own ownership/batch proof, not a zero-price shortcut.
            return False, []
    balance = 0.
    for when in sorted(balances):
        balance += balances[when]
        if balance < -1e-8:
            return False, []
    if not inbound or not outgoing or not target:
        return False, []
    return True, sorted({str(r['receipt']) for r in inbound})


def enrich(frame, query_fn=None, eligible_ids=None):
    if frame.empty or 'order_line_id' not in frame:
        return frame
    field = 'product_cost_unit' if 'product_cost_unit' in frame else 'source_cost'
    if field not in frame:
        return frame
    ids = sorted({int(r['order_line_id']) for r in frame.to_dict('records')
                  if number(r.get(field)) == 0 and present(r.get('order_line_id'))
                  and (eligible_ids is None or int(r['order_line_id']) in eligible_ids)})
    results, reader = {}, Reader(query_fn)
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset+100]
        params, placeholders = bindings(batch)
        try:
            contexts = reader.read(f"""
SELECT sl.id AS order_line_id,sl.product_id,sl.product_cost_unit AS raw_cost,sl.is_distribution,
       pp.default_code AS actual_ir,pp.tracking_method,pp.purchase_price,pt.tracking,
       f.main_upc,f.product_id AS function_product_id,
       fallback.fallback_matches,fallback.fallback_purchase_price
FROM public.sale_order_line sl JOIN public.product_product pp ON pp.id=sl.product_id
JOIN public.product_template pt ON pt.id=pp.product_tmpl_id
LEFT JOIN public.bi_profit_sale_product_info f ON f.order_line_id=sl.id
LEFT JOIN LATERAL (
  SELECT COUNT(*) AS fallback_matches,MIN(t.purchase_price) AS fallback_purchase_price
  FROM public.product_template t WHERE t.product_id=sl.product_id
) fallback ON TRUE
WHERE sl.id IN ({placeholders})
""", params)
            by_line = defaultdict(list)
            for r in contexts:
                by_line[int(r['order_line_id'])].append(r)
            valid = [rs[0] for rs in by_line.values() if len(rs) == 1 and rs[0].get('function_product_id') == rs[0]['product_id']]
            for line in batch:
                results[line] = {'status':'incomplete','code':'source_missing_or_duplicate','gift_verified':False,
                                 'summary':LABELS['source_missing_or_duplicate']}
            if not valid:
                continue
            params, _ = bindings([r['product_id'] for r in valid], 'p')
            for i, r in enumerate(valid):
                params[f'u{i}'] = r.get('main_upc') if present(r.get('main_upc')) else None
                params[f'l{i}'] = int(r['order_line_id'])
            values = ','.join(f'(:l{i},:p{i},CAST(:u{i} AS text))' for i in range(len(valid)))
            purchases = reader.read(f"""
WITH targets(line,product,main_upc) AS (VALUES {values})
SELECT t.line AS order_line_id,pp.id AS scope_product_id,pol.id AS po_line_id,
       po.name AS po_number,po.state AS po_state,pol.price_unit,pol.qty_received,rc.name AS currency
FROM targets t LEFT JOIN public.product_product pp
  ON (t.main_upc IS NULL AND pp.id=t.product) OR (t.main_upc IS NOT NULL AND pp.barcode=t.main_upc)
LEFT JOIN public.purchase_order_line pol ON pol.product_id=pp.id
LEFT JOIN public.purchase_order po ON po.id=pol.order_id
LEFT JOIN public.res_currency rc ON rc.id=pol.currency_id
""", params)
            by_target = defaultdict(list)
            for match in purchases:
                by_target[int(match['order_line_id'])].append(match)
            eligible = []
            for r in valid:
                line = int(r['order_line_id'])
                matches = by_target[line]
                product_ids = {m['scope_product_id'] for m in matches if present(m.get('scope_product_id'))}
                if len(product_ids) != 1:
                    continue
                po = [m for m in matches if present(m['po_line_id'])]
                results[line] = judge(r, po, [], [])
                if results[line]['code'] == 'verified_no_po_zero_cost' and can_check_gift(r):
                    eligible.append(r)
        except Exception:
            # Driver text can contain credentials.
            for line in batch:
                results[line] = {'status':'failed','code':'evidence_read_failed','gift_verified':False,
                                 'summary':LABELS['evidence_read_failed']}
            continue
        if not eligible:
            continue
        try:
            products = sorted({int(r['product_id']) for r in eligible})
            pparams, pvalues = bindings(products, 'product')
            moves = reader.read(f"""
SELECT sm.product_id,sm.id AS move_id,sm.state,sm.date AS move_date,sm.product_qty,sm.price_unit,
       sm.sale_line_id,sm.purchase_line_id,sm.created_purchase_line_id,sm.procurement_id,
       sm.inventory_id,sm.production_id,sm.origin_returned_move_id,sm.picking_id,
       src.usage AS source_usage,dst.usage AS dest_usage,sp.name AS receipt,sp.state AS receipt_state
FROM public.stock_move sm
LEFT JOIN public.stock_location src ON src.id=sm.location_id
LEFT JOIN public.stock_location dst ON dst.id=sm.location_dest_id
LEFT JOIN public.stock_picking sp ON sp.id=sm.picking_id
WHERE sm.product_id IN ({pvalues}) AND sm.state='done'
""", pparams)
            picks = sorted({int(r['picking_id']) for r in moves if present(r.get('picking_id'))
                            and r.get('source_usage') == 'supplier' and r.get('dest_usage') == 'internal'})
            messages = []
            if picks:
                mparams, mvalues = bindings(picks, 'pick')
                messages = reader.read(f"""
SELECT res_id AS picking_id,body FROM public.mail_message
WHERE model='stock.picking' AND res_id IN ({mvalues}) AND message_type='comment'
""", mparams)
            for r in eligible:
                results[int(r['order_line_id'])] = judge(r, [],
                    [m for m in moves if m['product_id']==r['product_id']], messages)
        except Exception:
            # Optional receipts do not invalidate a completed PO lookup.
            for r in eligible:
                results[int(r['order_line_id'])]['receipt_check_status'] = 'failed'
    result = frame.copy()
    if results:
        result['cost_review'] = [results.get(int(r['order_line_id'])) if present(r.get('order_line_id')) else None
                                 for r in result.to_dict('records')]
    return result


def public_reviews(frame):
    """Deduplicate overlapping rules without exposing database identities."""
    rows, seen = [], set()
    for r in frame.to_dict('records'):
        review = r.get('cost_review')
        if not isinstance(review, dict):
            continue
        key = r.get('order_line_id')
        if key in seen:
            continue
        seen.add(key)
        rows.append({**{k:str(r[k]) if present(r.get(k)) else None
                       for k in ('market_place','store','sku','order_number')}, **review})
    return rows
