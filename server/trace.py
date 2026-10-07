"""Internal-transaction (trace) extraction with graceful method support.

Public Ethereum RPCs differ in which debug/trace methods they expose:
  * trace_transaction (Parity/OpenEthereum/Erigon-style): list of flat frames
  * debug_traceTransaction (Geth-style, callTracer): nested call tree
We normalize either into a flat frame list. If the RPC answers with a method-not-found
error, we return None and the caller degrades cleanly. Parsing functions are pure so
they can be unit-tested on stub payloads without any network.
"""
from __future__ import annotations
from . import chain as _chain  # reuse quantity/eth helpers


def _frame_from_parity(action, rtype, result, error, depth):
    frm = action.get('from')
    to = action.get('to')
    raw_value = action.get('value', '0x0')
    try:
        value = int(raw_value, 16) if isinstance(raw_value, str) else 0
    except ValueError:
        value = 0
    call_type = action.get('callType') or rtype
    ok = error in (None, '') and isinstance(result, dict)
    return {'depth': depth, 'from': (frm or '').lower() or None,
            'to': (to or '').lower() or None, 'value_wei': str(value),
            'value_eth': _chain.eth(value), 'call_type': call_type,
            'success': bool(ok), 'error': error or None}


def parse_parity_trace(items, max_frames=200):
    if not isinstance(items, list):
        return None
    frames = []
    for item in items:
        if not isinstance(item, dict):
            continue
        action = item.get('action') or {}
        # depth approximated by subtraces nesting is not exposed flat; use 0 and mark count
        frames.append(_frame_from_parity(action, item.get('type', 'call'),
                                         item.get('result'), item.get('error'), 0))
        if len(frames) >= max_frames:
            break
    return frames


def _walk_calltracer(node, depth, out, max_frames):
    if not isinstance(node, dict):
        return
    raw_value = node.get('value', '0x0')
    try:
        value = int(raw_value, 16) if isinstance(raw_value, str) else 0
    except ValueError:
        value = 0
    out.append({'depth': depth, 'from': (node.get('from') or '').lower() or None,
                'to': (node.get('to') or '').lower() or None, 'value_wei': str(value),
                'value_eth': _chain.eth(value), 'call_type': node.get('type', 'call'),
                'success': not node.get('error'), 'error': node.get('error') or None})
    if len(out) >= max_frames:
        return
    for child in node.get('calls') or []:
        _walk_calltracer(child, depth + 1, out, max_frames)
        if len(out) >= max_frames:
            return


def parse_calltracer(root, max_frames=200):
    if not isinstance(root, dict):
        return None
    out = []
    _walk_calltracer(root, 0, out, max_frames)
    return out


def normalize_trace(method, result):
    """method is 'trace_transaction' or 'debug_traceTransaction'; result is RPC result."""
    if method == 'trace_transaction':
        return parse_parity_trace(result)
    if method == 'debug_traceTransaction':
        return parse_calltracer(result)
    return None


def unsupported(error_obj):
    """Heuristic: does an RPC error mean the method is unavailable on this node?"""
    if not isinstance(error_obj, dict):
        return False
    msg = str(error_obj.get('message', '')).lower()
    code = error_obj.get('code')
    needles = ('the method trace', 'the method debug', 'not exists', 'not supported',
               'method not found', 'unknown method', 'method not available')
    return any(n in msg for n in needles) or code == -32601


def summarize_frames(frames):
    """Aggregate internal ETH movements: per-frame total value, count, failures."""
    if not frames:
        return {'frame_count': 0, 'internal_eth_calls': 0,
                'total_internal_value_wei': '0', 'failed_frames': 0}
    total = sum(int(f['value_wei']) for f in frames)
    failed = sum(1 for f in frames if not f['success'])
    nonzero = sum(1 for f in frames if int(f['value_wei']) > 0)
    return {'frame_count': len(frames), 'internal_eth_calls': nonzero,
            'total_internal_value_wei': str(total),
            'total_internal_value_eth': _chain.eth(total), 'failed_frames': failed}
