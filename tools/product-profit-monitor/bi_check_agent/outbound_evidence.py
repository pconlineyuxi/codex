"""Resolve multi-line shipments from physical evidence, never MIN(line_id)."""
from collections import defaultdict
import pandas as pd
from bi_check_agent.evidence_reader import Reader
from bi_check_agent.patrol_policy import number, present


def resolved(event):
    return number(event.get('association_resolved')) == 1 or number(event.get('line_matches')) == 1


def resolved_rows(frame):
    return frame[frame.apply(resolved, axis=1)] if not frame.empty else frame


def same(a, b):
    a, b = number(a), number(b)
    return a is not None and b is not None and abs(a-b) <= 1e-8


def resolve(lines, scans, moves):
    """Inputs cover one order/product and one business day's scans/moves.

    SNs must identify a unique sale line and reconcile all positive movement
    quantities. Without SN, every candidate must be fully delivered on this day.
    Returns line IDs privately, not an invented allocation of scan quantities.
    """
    if not lines or not scans or not moves:
        return [], 'missing_evidence'
    if (len({r['order_line_id'] for r in lines}) != len(lines)
            or len({r['event_id'] for r in scans}) != len(scans)
            or any(not present(r.get('move_line_id')) for r in moves)
            or len({r['move_line_id'] for r in moves}) != len(moves)):
        return [], 'duplicate_or_missing_evidence'
    if any(number(r.get('scan_qty')) is None or r['scan_qty'] <= 0 for r in scans):
        return [], 'invalid_quantity'
    valid = {r['order_line_id']: r for r in lines}
    if any(r.get('state') != 'done' or r.get('source_usage') != 'internal'
           or r.get('dest_usage') != 'customer' or present(r.get('origin_returned_move_id'))
           or r.get('order_line_id') not in valid or number(r.get('qty_done')) is None
           or r['qty_done'] <= 0 for r in moves):
        return [], 'conflicting_movement'
    if not same(sum(r['scan_qty'] for r in scans), sum(r['qty_done'] for r in moves)):
        return [], 'quantity_mismatch'
    by_line = defaultdict(float)
    for r in moves:
        by_line[r['order_line_id']] += r['qty_done']
    if all(present(r.get('sn')) for r in scans):
        if len({r['sn'] for r in scans}) != len(scans):
            return [], 'duplicate_sn'
        if {r.get('sn') for r in moves} != {r['sn'] for r in scans}:
            return [], 'sn_mismatch'
        for scan in scans:
            matched = [r for r in moves if r.get('sn') == scan['sn']]
            if len(matched) != 1 or not same(matched[0]['qty_done'], scan['scan_qty']):
                return [], 'sn_not_unique'
        for line_id, qty in by_line.items():
            line = valid[line_id]
            if (number(line.get('qty_delivered')) is None or number(line.get('product_uom_qty')) is None
                    or qty > line['qty_delivered'] or qty > line['product_uom_qty']):
                return [], 'quantity_mismatch'
        return sorted(by_line), 'sn_stock_move'
    if any(present(r.get('sn')) for r in scans + moves):
        return [], 'mixed_sn_evidence'
    for line in lines:
        qty = number(line.get('product_uom_qty'))
        if qty is None or qty <= 0 or not same(qty, line.get('qty_delivered')) or not same(qty, by_line.get(line['order_line_id'])):
            return [], 'partial_or_quantity_mismatch'
    return sorted(valid), 'full_quantity_stock_move'


def enrich(cost, start, end, query_fn=None):
    if cost.empty or not (cost.line_matches > 1).any():
        return cost
    reader, additions = Reader(query_fn), {}
    candidates = cost[cost.line_matches > 1].to_dict('records')
    for offset in range(0, len(candidates), 100):
        batch = candidates[offset:offset+100]
        params = {'start': start, 'end': end}
        for i, r in enumerate(batch):
            params[f'o{i}'], params[f'p{i}'] = int(r['order_id']), int(r['product_id'])
        pairs = ','.join(f'(:o{i},:p{i})' for i in range(len(batch)))
        try:
            lines = reader.read(f"""
SELECT sl.order_id,sl.product_id,sl.id AS order_line_id,sl.product_uom_qty,sl.qty_delivered,pp.default_code AS actual_ir
FROM public.sale_order_line sl JOIN public.product_product pp ON pp.id=sl.product_id
WHERE (sl.order_id,sl.product_id) IN ({pairs})
""", params)
            scans = reader.read(f"""
SELECT h.order_id,h.product_id,h.id AS event_id,h.sn,h.scan_qty
FROM public.order_outbound_history h
WHERE (h.order_id,h.product_id) IN ({pairs}) AND h.scan_time>=:start AND h.scan_time<:end AND h.scan_qty>0
""", params)
            moves = reader.read(f"""
SELECT sl.order_id,sl.product_id,sl.id AS order_line_id,sm.state,sm.origin_returned_move_id,
       sml.id AS move_line_id,sml.qty_done,lot.name AS sn,src.usage AS source_usage,dst.usage AS dest_usage
FROM public.sale_order_line sl JOIN public.stock_move sm ON sm.sale_line_id=sl.id AND sm.product_id=sl.product_id
LEFT JOIN public.stock_move_line sml ON sml.move_id=sm.id AND sml.product_id=sm.product_id
LEFT JOIN public.stock_production_lot lot ON lot.id=sml.lot_id AND lot.product_id=sm.product_id
LEFT JOIN public.stock_location src ON src.id=sm.location_id
LEFT JOIN public.stock_location dst ON dst.id=sm.location_dest_id
WHERE (sl.order_id,sl.product_id) IN ({pairs}) AND sm.state='done' AND sm.date>=:start AND sm.date<:end
""", params)
            for event in batch:
                key = (event['order_id'], event['product_id'])
                select = lambda records: [r for r in records if (r['order_id'],r['product_id']) == key]
                chosen = select(lines)
                ids, reason = resolve(chosen, select(scans), select(moves))
                if len(chosen) != int(event['line_matches']):
                    ids, reason = [], 'candidate_count_changed'
                common = {**event, 'association_resolved': bool(ids), 'association_basis': reason,
                          'actual_ir': chosen[0]['actual_ir'] if chosen else None}
                additions[key] = [{**common, 'order_line_id': i} for i in ids] if ids else [common]
        except Exception:
            for event in batch:
                additions[(event['order_id'],event['product_id'])] = [{**event, 'association_resolved': False,
                    'association_basis': 'evidence_read_failed'}]
    rows = []
    for r in cost.to_dict('records'):
        rows.extend(additions.get((r['order_id'],r['product_id']), [r]))
    return pd.DataFrame(rows)


def enrich_source(frame, query_fn=None):
    """Reuse the resolver for interactive probes; do not guess older shipments.

    The lookup supplies the last positive scan of this order/product. A line
    is confirmed only if evidence from that scan's business day names it.
    """
    required = {'raw_order_id','product_id','order_line_id','line_matches','last_scan_at'}
    if frame.empty or not required <= set(frame):
        return frame
    from bi_check_agent.lifecycle_patrol import utc_bounds, ZONE
    batches = defaultdict(list)
    for row in frame.to_dict('records'):
        if number(row['line_matches']) is None or row['line_matches'] <= 1 or not present(row['last_scan_at']):
            continue
        scan = pd.Timestamp(row['last_scan_at'])
        scan = scan.tz_localize('UTC') if scan.tzinfo is None else scan.tz_convert('UTC')
        batches[scan.tz_convert(ZONE).date()].append({**row, 'order_id': row['raw_order_id']})
    evidence = {}
    for day, rows in batches.items():
        costs = pd.DataFrame(rows).drop_duplicates(['order_id','product_id'])
        checked = enrich(costs, *utc_bounds(day), query_fn)
        for r in checked.to_dict('records'):
            if resolved(r):
                evidence[int(r['order_line_id'])] = r.get('association_basis')
    result = frame.copy()
    result['association_resolved'] = [int(i) in evidence for i in result.order_line_id]
    result['association_basis'] = [evidence.get(int(i)) for i in result.order_line_id]
    return result
