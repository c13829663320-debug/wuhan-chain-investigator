"""Pure decoding of well-known EVM event logs (ERC-20 Transfer/Approval).

All functions are pure: given a raw receipt log dict {address, topics, data}
they return a structured event or None. No network, no state. Known event
signatures:

  Transfer(address indexed from, address indexed to, uint256 value)
    topic0 = keccak256('Transfer(address,address,uint256)')
  Approval(address indexed owner, address indexed spender, uint256 value)
    topic0 = keccak256('Approval(address,address,uint256)')
"""
from __future__ import annotations
from . import labels

TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
APPROVAL_TOPIC = '0x8c5be1e5ebec7d5bd14f71427d34b3452b946f5121e3499c5886e704f7016b1'

_UINT256_MAX = 2 ** 256 - 1


def _hex_int(hexstr):
    if not isinstance(hexstr, str) or not hexstr.startswith('0x'):
        return None
    try:
        v = int(hexstr, 16)
    except ValueError:
        return None
    return v if 0 <= v <= _UINT256_MAX else None


def _topic_address(topic):
    """A 32-byte topic that holds an address is 12 zero bytes + 20 address bytes."""
    if not isinstance(topic, str) or len(topic) != 66 or not topic.startswith('0x'):
        return None
    if topic[2:26] != '0' * 24:
        return None
    return '0x' + topic[26:66]


def decode_log(log):
    """Decode one raw receipt log. Returns a structured event dict or None.

    Expected log shape: {'address': '0x..40', 'topics': ['0x..64', ...], 'data': '0x..'}.
    Unknown topics are ignored (return None) rather than guessed.
    """
    if not isinstance(log, dict):
        return None
    addr = log.get('address')
    topics = log.get('topics')
    data = log.get('data')
    if not isinstance(addr, str) or not isinstance(topics, list) or not isinstance(data, str):
        return None
    if not topics:
        return None
    topic0 = topics[0].lower()
    token = labels.token_meta(addr)
    base = {
        'contract': addr.lower(),
        'token_symbol': token['symbol'] if token else None,
        'token_name': token['name'] if token else None,
        'token_decimals': token['decimals'] if token else None,
        'log_index': log.get('logIndex'),
        'raw_topic0': topic0,
    }
    if topic0 == TRANSFER_TOPIC and len(topics) == 3 and isinstance(data, str):
        frm = _topic_address(topics[1])
        to = _topic_address(topics[2])
        value = _hex_int(data)
        if frm is None or to is None or value is None:
            return None
        dec = token['decimals'] if token else 18
        return {**base, 'event': 'Transfer', 'from': frm, 'to': to,
                'value_wei': str(value), 'value_human': labels.human_amount(value, dec)}
    if topic0 == APPROVAL_TOPIC and len(topics) == 3 and isinstance(data, str):
        owner = _topic_address(topics[1])
        spender = _topic_address(topics[2])
        allowance = _hex_int(data)
        if owner is None or spender is None or allowance is None:
            return None
        dec = token['decimals'] if token else 18
        return {**base, 'event': 'Approval', 'owner': owner, 'spender': spender,
                'allowance_wei': str(allowance),
                'allowance_human': labels.human_amount(allowance, dec),
                'unlimited': allowance == _UINT256_MAX}
    return None


def decode_receipt_logs(receipt):
    """Decode every ERC-20 event in a receipt's logs. Unknown logs are skipped."""
    out = []
    logs = receipt.get('logs') if isinstance(receipt, dict) else None
    if not isinstance(logs, list):
        return out
    for raw in logs:
        ev = decode_log(raw)
        if ev is not None:
            out.append(ev)
    return out


def label_address(addr):
    """Decorate an address string with a public label if known (pure)."""
    return labels.label_of(addr)
