"""LLM "investigation reasoning officer" adapter (OpenAI-compatible).

Reads config from environment:
  JIANGCHENG_LLM_BASE_URL  e.g. https://api.openai.com/v1
  JIANGCHENG_LLM_API_KEY   bearer token
  JIANGCHENG_LLM_MODEL     e.g. gpt-4o-mini
  JIANGCHENG_LLM_TIMEOUT   optional seconds (default 8)

When no key is configured (or the call fails / times out), we fall back to a
deterministic, evidence-only rule synthesizer and clearly mark `engine`. The LLM
may only reason OVER the supplied evidence packet — we never ask it to invent
on-chain facts, and we ask it to state uncertainty and manual checks.
"""
from __future__ import annotations
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SYSTEM_PROMPT = (
    '你是以太坊链上异动调查官。你只能基于用户提供的结构化链上证据（交易/回执指标、'
    '解码出的 ERC-20 Transfer/Approval 日志、内部调用 trace、地址标签、资金流聚合）进行推理。'
    '禁止编造未在证据中出现的区块、地址、金额或事件。对每条解释，给出：假设、置信度(high/medium/low)、'
    '支撑证据字段、备选解释、需要人工跨源核验的问题、仍不确定的点。'
    '以 JSON 返回：{"hypotheses":[{"title":"","confidence":"","evidence":[],"alternatives":[],"cross_source_checks":[],"uncertainties":[]}],"overall":"","open_questions":[]}。'
)


def config_from_env():
    return {
        'base_url': os.environ.get('JIANGCHENG_LLM_BASE_URL', '').rstrip('/'),
        'api_key': os.environ.get('JIANGCHENG_LLM_API_KEY', ''),
        'model': os.environ.get('JIANGCHENG_LLM_MODEL', ''),
        'timeout': float(os.environ.get('JIANGCHENG_LLM_TIMEOUT', '8')),
    }


def available(cfg=None):
    cfg = cfg or config_from_env()
    return bool(cfg['base_url'] and cfg['api_key'] and cfg['model'])


def _build_user_packet(evidence):
    # Trim to a bounded, serializable summary so we never ship a multi-MiB prompt.
    slim = {
        'summary': evidence.get('summary'),
        'sampled_transactions': [
            {k: t.get(k) for k in ('hash', 'from', 'to', 'status', 'value_eth',
                                    'actual_fee_eth', 'gas_use_ratio_percent', 'selector')}
            for t in evidence.get('sampled_transactions', [])
        ],
        'findings': evidence.get('findings', []),
        'events': evidence.get('events', []),
        'trace_summary': evidence.get('trace_summary'),
        'fund_flow': evidence.get('fund_flow', []),
        'address_labels': evidence.get('address_labels', []),
    }
    return json.dumps(slim, ensure_ascii=False)[:12000]


def _post_chat(cfg, messages, transport=None):
    body = json.dumps({'model': cfg['model'], 'messages': messages,
                       'temperature': 0.1, 'response_format': {'type': 'json_object'}}).encode()
    url = cfg['base_url'] + '/chat/completions'
    req = Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + cfg['api_key'],
        'User-Agent': 'JiangchengChainInvestigator-LLM/1.0',
    })
    if transport is not None:
        return transport(req, cfg)
    try:
        with urlopen(req, timeout=cfg['timeout']) as resp:
            raw = resp.read(64 * 1024)
        data = json.loads(raw)
        return data['choices'][0]['message']['content']
    except (HTTPError, URLError, TimeoutError, OSError, KeyError, IndexError, ValueError) as exc:
        raise RuntimeError('LLM call failed: %r' % (exc,)) from exc


def deterministic_fallback(evidence):
    """Evidence-only rule reasoning used when no LLM is configured.

    Mirrors the existing deterministic explain() but re-expressed as a ranked,
    labeled hypothesis list suitable for the 'reasoning officer' view.
    """
    findings = evidence.get('findings', [])
    events = evidence.get('events', [])
    grouped = {}
    for f in findings:
        grouped.setdefault(f['transaction_hash'], []).append(f)
    hypotheses = []
    for txhash, items in grouped.items():
        labels_hit = [i['label'] for i in items]
        ev = ['%s (%s)' % (i['rule_id'], ','.join(i['evidence_fields'])) for i in items]
        alts = []
        checks = []
        for i in items:
            if i['rule_id'] == 'FAILED':
                alts.append('合约业务 revert 或资源不足；status=0 不等于攻击。')
                checks.append('需要 trace/revert reason 与已验证合约源码确认。')
            if i['rule_id'] == 'LARGE_VALUE':
                alts.append('可能是正常大额结算/归集；也可能是待复核转移。')
                checks.append('核对内部调用、退款与代币净流，再判断净资金流。')
            if i['rule_id'] == 'MAX_APPROVAL':
                alts.append('可能是预期长期授权；选择器也可能不是 ERC-20。')
                checks.append('读实际 allowance 与历史授权，确认暴露面。')
        # attach decoded ERC-20 evidence
        tx_events = [e for e in events if e.get('_tx_hash') == txhash]
        if tx_events:
            ev.append('decoded_events=%d' % len(tx_events))
            checks.append('跨源核对代币合约标签与余额变化。')
        hypotheses.append({'title': ' / '.join(labels_hit),
                           'confidence': 'low',
                           'evidence': ev,
                           'alternatives': list(dict.fromkeys(alts)),
                           'cross_source_checks': list(dict.fromkeys(checks)),
                           'uncertainties': ['确定性规则结论，未接入 LLM 推理，需人工复核。']})
    hypotheses.sort(key=lambda h: -len(h['evidence']))
    return {'engine': 'deterministic_fallback', 'model': None,
            'note': '未配置 JIANGCHENG_LLM_API_KEY；以下为基于所给证据的确定性排序结论。',
            'hypotheses': hypotheses,
            'overall': '仅基于所给链上证据；未推断真实主体、控制人或攻击归因。',
            'open_questions': ['是否存在未采集到的同批次授权/转移？', '地址标签是否需要跨源核实？']}


def reason(evidence, cfg=None, transport=None):
    """Produce ranked hypotheses from evidence. Never raises; always returns a dict."""
    cfg = cfg or config_from_env()
    if not available(cfg):
        out = deterministic_fallback(evidence)
        return out
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': _build_user_packet(evidence)}]
    try:
        content = _post_chat(cfg, messages, transport=transport)
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or 'hypotheses' not in parsed:
            raise ValueError('shape')
        parsed['engine'] = 'openai_compatible_llm'
        parsed['model'] = cfg['model']
        parsed.setdefault('note', '')
        parsed['note'] = 'LLM 仅基于所给证据推理；置信度与人工核验项需复核。' + parsed['note']
        return parsed
    except Exception:
        out = deterministic_fallback(evidence)
        out['note'] += ' 调用 LLM 失败或超时，已降级为确定性结论。'
        return out
