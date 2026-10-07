"""Aggregate per-address movement across decoded ERC-20 events and internal ETH frames.

Pure: given decoded events (from decoder.decode_receipt_logs) and internal frames
(from trace.normalize_trace), produce a bounded per-address flow table. This is a
narrow, single-transaction (or small sample) view — NOT a full chain-wide tracker.
"""
from __future__ import annotations
from . import labels


def _bucket(flows, addr, asset, direction, amount_wei, human):
    if not addr:
        return
    key = addr
    slot = flows.setdefault(key, {'address': addr, 'label': None, 'tokens': {}, 'eth': {'in_wei': 0, 'out_wei': 0}})
    lab = labels.label_of(addr)
    if lab and slot['label'] is None:
        slot['label'] = lab['label']
    if asset == 'ETH':
        slot['eth'][direction + '_wei'] += amount_wei
        return
    tok = slot['tokens'].setdefault(asset, {'symbol': None, 'in_wei': 0, 'out_wei': 0, 'in_human': '0', 'out_human': '0', 'decimals': 18})
    if human is not None:
        tok['symbol'] = tok['symbol']  # keep as set below
    tok[direction + '_wei'] += amount_wei


def aggregate(decoded_events, internal_frames, tx_outer):
    """tx_outer: list of {from,to,value_wei,status} for outer ETH transfers."""
    flows = {}
    for ev in decoded_events or []:
        asset = ev['contract']
        dec = ev.get('token_decimals') or 18
        sym = ev.get('token_symbol')
        if ev['event'] == 'Transfer':
            amt = int(ev['value_wei'])
            _bucket(flows, ev['from'], asset, 'out', amt, ev['value_human'])
            _bucket(flows, ev['to'], asset, 'in', amt, ev['value_human'])
            for slot in flows.values():
                if asset in slot['tokens']:
                    slot['tokens'][asset]['symbol'] = sym
                    slot['tokens'][asset]['decimals'] = dec
    for fr in internal_frames or []:
        if int(fr['value_wei']) > 0:
            v = int(fr['value_wei'])
            _bucket(flows, fr['from'], 'ETH', 'out', v, fr['value_eth'])
            _bucket(flows, fr['to'], 'ETH', 'in', v, fr['value_eth'])
    for tx in tx_outer or []:
        v = int(tx['value_wei']) if tx.get('status') == 1 else 0
        if v > 0:
            _bucket(flows, tx['from'], 'ETH', 'out', v, None)
            _bucket(flows, tx['to'], 'ETH', 'in', v, None)
    rows = []
    for slot in flows.values():
        eth_in, eth_out = slot['eth']['in_wei'], slot['eth']['out_wei']
        token_rows = []
        for addr_token, t in slot['tokens'].items():
            t = dict(t)
            t['asset'] = addr_token
            t['in_human'] = labels.human_amount(t['in_wei'], t['decimals'])
            t['out_human'] = labels.human_amount(t['out_wei'], t['decimals'])
            token_rows.append(t)
        rows.append({'address': slot['address'], 'label': slot['label'],
                     'eth_in_wei': str(eth_in), 'eth_out_wei': str(eth_out),
                     'eth_net_wei': str(eth_in - eth_out),
                     'tokens': token_rows})
    rows.sort(key=lambda r: (-abs(int(r['eth_net_wei'])), r['address']))
    return rows[:64]
