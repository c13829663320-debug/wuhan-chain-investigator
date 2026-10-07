"""Unit tests for the deep-investigation layer: ERC-20 log decoding, trace parsing,
address labels, fund-flow aggregation, the LLM adapter (stubbed), and the bundled
real on-chain case. No network and no LLM key is required."""
import json
import os
import unittest
from server import chain, decoder, trace as trace_mod, fundflow, labels, llm


USDC = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'
WETH = '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2'
FROM = '0x1111111111111111111111111111111111111111'
TO = '0x2222222222222222222222222222222222222222'
SPENDER = '0x7a250d5630b4cf539739df2c5dacb4c659f2488d'  # Uniswap V2 Router02


def pad32(addr):
    return '0x' + '0' * 24 + addr[2:]


class DecoderTests(unittest.TestCase):
    def test_transfer_known_vector(self):
        # 1.5 USDC = 1_500_000 base units (6 decimals)
        value = 1_500_000
        log = {'address': USDC,
               'topics': [decoder.TRANSFER_TOPIC, pad32(FROM), pad32(TO)],
               'data': '0x' + value.to_bytes(32, 'big').hex(),
               'logIndex': '0x0'}
        ev = decoder.decode_log(log)
        self.assertEqual(ev['event'], 'Transfer')
        self.assertEqual(ev['from'], FROM)
        self.assertEqual(ev['to'], TO)
        self.assertEqual(ev['token_symbol'], 'USDC')
        self.assertEqual(ev['token_decimals'], 6)
        self.assertEqual(ev['value_wei'], str(value))
        self.assertEqual(ev['value_human'], '1.5')

    def test_approval_unlimited_known_vector(self):
        max256 = 2 ** 256 - 1
        log = {'address': WETH,
               'topics': [decoder.APPROVAL_TOPIC, pad32(FROM), pad32(SPENDER)],
               'data': '0x' + max256.to_bytes(32, 'big').hex(),
               'logIndex': '0x1'}
        ev = decoder.decode_log(log)
        self.assertEqual(ev['event'], 'Approval')
        self.assertEqual(ev['owner'], FROM)
        self.assertEqual(ev['spender'], SPENDER)
        self.assertTrue(ev['unlimited'])
        self.assertEqual(ev['token_symbol'], 'WETH')

    def test_unknown_topic_ignored(self):
        weird = {'address': USDC, 'topics': ['0x' + '11' * 32], 'data': '0x', 'logIndex': '0x0'}
        self.assertIsNone(decoder.decode_log(weird))

    def test_bad_padding_rejected(self):
        # topic that is not an address-shaped 32 bytes
        bad = {'address': USDC,
               'topics': [decoder.TRANSFER_TOPIC, '0x' + 'ff' * 32, pad32(TO)],
               'data': '0x00' * 32, 'logIndex': '0x0'}
        self.assertIsNone(decoder.decode_log(bad))

    def test_decode_receipt_logs_skips_unknown(self):
        rc = {'logs': [
            {'address': USDC, 'topics': [decoder.TRANSFER_TOPIC, pad32(FROM), pad32(TO)],
             'data': '0x' + (10**6).to_bytes(32, 'big').hex(), 'logIndex': '0x0'},
            {'address': '0x' + '33' * 20, 'topics': ['0x' + '22' * 32], 'data': '0x', 'logIndex': '0x1'},
        ]}
        out = decoder.decode_receipt_logs(rc)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['token_symbol'], 'USDC')


class TraceTests(unittest.TestCase):
    def test_parity_trace_normalized(self):
        items = [
            {'type': 'call', 'action': {'from': FROM, 'to': TO, 'value': '0xde0b6b3a7640000', 'callType': 'call'},
             'result': {'gasUsed': '0x5208'}},
            {'type': 'call', 'action': {'from': TO, 'to': FROM, 'value': '0x0', 'callType': 'call'},
             'error': 'out of gas'},
        ]
        frames = trace_mod.parse_parity_trace(items)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]['value_eth'], '1')
        self.assertTrue(frames[0]['success'])
        self.assertFalse(frames[1]['success'])
        s = trace_mod.summarize_frames(frames)
        self.assertEqual(s['internal_eth_calls'], 1)
        self.assertEqual(s['failed_frames'], 1)

    def test_calltracer_nested(self):
        root = {'from': FROM, 'to': TO, 'value': '0x10', 'type': 'CALL',
                'calls': [{'from': TO, 'to': SPENDER, 'value': '0x5', 'type': 'CALL',
                           'calls': []}]}
        frames = trace_mod.parse_calltracer(root)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[1]['depth'], 1)

    def test_unsupported_detection(self):
        self.assertTrue(trace_mod.unsupported({'code': -32601, 'message': 'the method trace_notFound does not exist'}))
        self.assertFalse(trace_mod.unsupported({'code': -32000, 'message': 'execution aborted'}))
        # normalize_trace on unknown method returns None (graceful)
        self.assertIsNone(trace_mod.normalize_trace('eth_bogus', [1, 2]))


class LabelTests(unittest.TestCase):
    def test_known_protocol_label(self):
        lab = labels.label_of(SPENDER)
        self.assertIsNotNone(lab)
        self.assertIn('Uniswap', lab['label'])

    def test_unknown_address_no_invented_identity(self):
        self.assertIsNone(labels.label_of('0x' + 'ab' * 20))

    def test_token_meta_and_human_amount(self):
        meta = labels.token_meta(USDC)
        self.assertEqual(meta['decimals'], 6)
        self.assertEqual(labels.human_amount(1_500_000, 6), '1.5')
        self.assertEqual(labels.human_amount(10**18, 18), '1')


class FundFlowTests(unittest.TestCase):
    def test_aggregates_transfers_and_eth(self):
        events = [
            {'event': 'Transfer', 'contract': USDC, 'from': FROM, 'to': TO,
             'value_wei': '2000000', 'value_human': '2', 'token_symbol': 'USDC', 'token_decimals': 6},
            {'event': 'Transfer', 'contract': WETH, 'from': TO, 'to': FROM,
             'value_wei': str(10**18), 'value_human': '1', 'token_symbol': 'WETH', 'token_decimals': 18},
        ]
        frames = [{'from': FROM, 'to': TO, 'value_wei': str(5 * 10**18), 'value_eth': '5', 'success': True}]
        outer = [{'from': FROM, 'to': TO, 'value_wei': str(10**18), 'status': 1}]
        rows = fundflow.aggregate(events, frames, outer)
        by = {r['address']: r for r in rows}
        self.assertEqual(by[FROM]['eth_out_wei'], str(5 * 10**18 + 10**18))
        self.assertEqual(by[TO]['eth_in_wei'], str(5 * 10**18 + 10**18))
        usdc_out = [t for t in by[FROM]['tokens'] if t['asset'] == USDC]
        self.assertEqual(usdc_out[0]['out_human'], '2')


class LlmAdapterTests(unittest.TestCase):
    def test_no_key_is_deterministic_fallback(self):
        out = llm.reason({'summary': {}, 'findings': [], 'sampled_transactions': []},
                         cfg={'base_url': '', 'api_key': '', 'model': '', 'timeout': 1})
        self.assertEqual(out['engine'], 'deterministic_fallback')

    def test_stubbed_llm_response(self):
        def fake_transport(req, cfg):
            self.assertEqual(req.full_url, 'https://llm.test/v1/chat/completions')
            return json.dumps({'hypotheses': [{'title': 'swap routed via 3 pools', 'confidence': 'medium',
                                               'evidence': ['events=5'], 'alternatives': ['normal arb'],
                                               'cross_source_checks': ['verify pool reserves'],
                                               'uncertainties': ['unknown token']}],
                               'overall': 'multi-hop DEX swap'})
        cfg = {'base_url': 'https://llm.test/v1', 'api_key': 'sk-test', 'model': 'mock-model', 'timeout': 1}
        out = llm.reason({'summary': {}, 'findings': [], 'sampled_transactions': []},
                         cfg=cfg, transport=fake_transport)
        self.assertEqual(out['engine'], 'openai_compatible_llm')
        self.assertEqual(out['model'], 'mock-model')
        self.assertEqual(out['hypotheses'][0]['confidence'], 'medium')

    def test_transport_failure_falls_back(self):
        def boom(req, cfg):
            raise OSError('network down')
        cfg = {'base_url': 'https://llm.test/v1', 'api_key': 'sk-test', 'model': 'mock', 'timeout': 1}
        out = llm.reason({'summary': {}, 'findings': [], 'sampled_transactions': []},
                         cfg=cfg, transport=boom)
        self.assertEqual(out['engine'], 'deterministic_fallback')
        self.assertIn('降级', out['note'])


class RealCaseTests(unittest.TestCase):
    def test_bundled_real_case_loads_and_decodes(self):
        r = chain.investigate({'mode': 'case'})
        deep = r['output']['deep']
        self.assertGreater(deep['decoded_event_count'], 0)
        # the captured swap contains WETH + USDC transfers
        symbols = {e.get('token_symbol') for e in deep['events']}
        self.assertIn('WETH', symbols)
        self.assertGreater(len(deep['fund_flow']), 0)
        # bundled case replays identically (offline)
        self.assertTrue(chain.verify({'receipt': r})['valid'])
        # no trace was available from the public node -> graceful note
        self.assertIn(deep['trace_summary']['frame_count'], (0, 1))


if __name__ == '__main__':
    unittest.main()
