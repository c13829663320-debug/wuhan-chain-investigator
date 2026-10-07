import copy
import json
import time
import unittest
from unittest.mock import patch
from server import chain
from server.errors import APIError

class ChainTests(unittest.TestCase):
    def setUp(self):
        chain.CACHE.clear()
        self.at=int(time.time())
        self.raw=chain.demo_fixture(self.at)
    def reject(self,raw):
        with self.assertRaises(APIError):chain.build_receipt(raw,'demo')
    def rehash(self,r):
        r['receipt_hash']=chain.digest({k:v for k,v in r.items() if k!='receipt_hash'});return r
    def live_sample(self):
        raw=copy.deepcopy(self.raw);raw['selection']='finalized_sample';return raw
    def test_demo_exact_values_and_replay(self):
        receipt=chain.build_receipt(self.raw,'demo')
        self.assertEqual(receipt['output']['summary'],{'sample_count':3,'success_count':2,'failed_count':1,'finding_count':5,'attempted_value_eth':'270','settled_outer_value_eth':'120','actual_fee_eth':'0.000166000000166'})
        failed=receipt['output']['sampled_transactions'][0]
        self.assertEqual(failed['actual_fee_eth'],'0.0001000000001')
        self.assertEqual(failed['settled_outer_value_eth'],'0')
        self.assertEqual(failed['value_eth'],'150')
        self.assertTrue(chain.verify({'receipt':receipt})['valid'])
        self.assertEqual(len(receipt['execution_trace']),5)
        self.assertTrue(all(t['explorer_url'] is None for t in receipt['output']['sampled_transactions']))
    def test_precise_integer_and_ratio_math(self):
        self.assertEqual(chain.eth(10**18+1),'1.000000000000000001')
        self.assertEqual(chain.percent(2,3),'66.67')
        self.assertIsNone(chain.percent(1,0))
        self.assertEqual(chain.percent(3,3),'100.00')
    def test_blob_fee_included(self):
        tx=self.raw['transactions'][1];r=self.raw['receipts'][1];tx['type']='0x3';r['blobGasUsed']='0x20000';r['blobGasPrice']='0x3'
        result=chain.build_receipt(self.raw,'demo')['output']['sampled_transactions'][1]
        self.assertEqual(result['blob_fee_wei'],'393216')
        self.assertEqual(int(result['actual_fee_wei']),21000*1000000001+393216)
    def test_missing_blob_price_rejected(self):
        self.raw['transactions'][1]['type']='0x3';self.reject(self.raw)
    def test_nonblob_receipt_blob_rejected(self):
        self.raw['receipts'][1].update(blobGasUsed='0x1',blobGasPrice='0x1');self.reject(self.raw)
    def test_wrong_chain_rejected(self):
        self.raw['chain_id']=2;self.reject(self.raw)
    def test_boolean_chain_rejected(self):
        self.raw['chain_id']=True;self.reject(self.raw)
    def test_receipt_transaction_mismatch_rejected(self):
        self.raw['receipts'][0]['transactionHash']='0x'+'ff'*32;self.reject(self.raw)
    def test_receipt_index_mismatch_rejected(self):
        self.raw['receipts'][0]['transactionIndex']='0x1';self.reject(self.raw)
    def test_receipt_block_mismatch_rejected(self):
        self.raw['receipts'][0]['blockHash']='0x'+'ff'*32;self.reject(self.raw)
    def test_unfinalized_rejected(self):
        self.raw['finalized']['number']=290;self.reject(self.raw)
    def test_stale_checkpoint_rejected(self):
        self.raw['finalized']['timestamp']=self.at-1801;self.reject(self.raw)
    def test_zero_denominator_rejected(self):
        self.raw['transactions'][0]['gas']='0x0';self.reject(self.raw)
    def test_overused_gas_rejected(self):
        self.raw['receipts'][1]['gasUsed']='0xfffff';self.reject(self.raw)
    def test_missing_field_rejected(self):
        del self.raw['receipts'][1]['effectiveGasPrice'];self.reject(self.raw)
    def test_invalid_status_rejected(self):
        self.raw['receipts'][1]['status']='0x2';self.reject(self.raw)
    def test_duplicate_transaction_rejected(self):
        self.raw['transactions'][1]=copy.deepcopy(self.raw['transactions'][0]);self.reject(self.raw)
    def test_unknown_snapshot_fields_rejected(self):
        self.raw['arbitrary']='ignored';self.reject(self.raw)
    def test_historical_finalized_transaction_accepted(self):
        raw=self.live_sample();tx=raw['transactions'][1];rc=raw['receipts'][1]
        raw.update(selection='transaction',requested_hash=tx['hash'],transactions=[tx],receipts=[rc])
        raw['block']['selected_transaction_hashes']=[tx['hash']]
        raw['block']['timestamp']=self.at-365*86400
        raw['finalized'].update(number=20000000,hash='0x'+'cd'*32,timestamp=self.at-600)
        r=chain.build_receipt(raw,'live')
        self.assertTrue(r['output']['source']['historical_transaction'])
        self.assertGreater(r['output']['source']['block_age_seconds'],86400)
        self.assertTrue(chain.verify({'receipt':r})['valid'])
    def test_old_receipt_replay_uses_collection_time(self):
        raw=chain.demo_fixture(self.at-365*86400)
        self.assertTrue(chain.verify({'receipt':chain.build_receipt(raw,'demo')})['valid'])
    def test_recomputed_hash_cannot_hide_derived_tampering(self):
        for path in [('output','summary','actual_fee_eth'),('output','source','collected_at'),('execution_trace',0,'output_hash'),('formulas',0,'formula'),('output','hypotheses',0,'title'),('output','timeline',0,'detail')]:
            r=chain.build_receipt(self.raw,'demo');node=r
            for part in path[:-1]:node=node[part]
            node[path[-1]]='tampered';self.rehash(r)
            result=chain.verify({'receipt':r})
            self.assertTrue(result['receipt_hash_valid']);self.assertFalse(result['rules_replay_valid']);self.assertFalse(result['valid'])
    def test_hash_tamper_rejected(self):
        r=chain.build_receipt(self.raw,'demo');r['receipt_hash']='0'*64
        self.assertFalse(chain.verify({'receipt':r})['valid'])
    def test_future_collection_rejected_on_replay(self):
        r=chain.build_receipt(chain.demo_fixture(self.at+3600),'demo');self.assertFalse(chain.verify({'receipt':r})['valid'])
    def test_size_bound(self):
        r=chain.build_receipt(self.raw,'demo');r['extra']='x'*(chain.MAX_RECEIPT+1)
        with self.assertRaises(APIError) as ctx:chain.verify({'receipt':r})
        self.assertEqual(ctx.exception.status,413)
    def test_calldata_bound(self):
        self.raw['transactions'][0]['input']='0x'+'ff'*(chain.MAX_INPUT_BYTES+1);self.reject(self.raw)
    def test_invalid_user_inputs(self):
        for body in [{'mode':'live','transaction_hash':'0x'+'gg'*32},{'mode':'live','transaction_hash':'0x12'},{'mode':'demo','transaction_hash':'0x'+'00'*32},{'mode':'live','url':'https://evil.test'},{'mode':[]},{'mode':None},{}]:
            with self.subTest(body=body),self.assertRaises(APIError):chain.investigate(body)
    def rpc_objects(self):
        raw=self.live_sample();b=raw['block']
        block={'number':hex(b['number']),'hash':b['hash'],'timestamp':hex(b['timestamp']),'transactions':copy.deepcopy(raw['transactions'])}
        return block,copy.deepcopy(raw['receipts'])
    def test_mocked_latest_rpc_path_and_cache(self):
        block,receipts=self.rpc_objects()
        with patch.object(chain,'rpc_batch',side_effect=[['0x1',block],receipts]) as rpc:
            r=chain.investigate({'mode':'live'});again=chain.investigate({'mode':'live'})
            self.assertEqual(r,again);self.assertEqual(rpc.call_count,2)
            self.assertTrue(chain.verify({'receipt':r})['valid'])
    def test_mocked_specific_hash_and_canonical_membership(self):
        block,receipts=self.rpc_objects();tx=block['transactions'][1];canonical=copy.deepcopy(block);canonical['transactions']=[t['hash'] for t in block['transactions']]
        with patch.object(chain,'rpc_batch',side_effect=[['0x1',block,tx,receipts[1]],[canonical]]):
            r=chain.investigate({'mode':'live','transaction_hash':tx['hash'].upper().replace('0X','0x')})
            self.assertEqual(r['input']['selection'],'transaction');self.assertEqual(r['output']['summary']['sample_count'],1)
    def test_pending_transaction_rejected(self):
        block,receipts=self.rpc_objects();tx=block['transactions'][0];tx['blockNumber']=None
        with patch.object(chain,'rpc_batch',return_value=['0x1',block,tx,None]),self.assertRaises(APIError):chain.investigate({'mode':'live','transaction_hash':tx['hash']})
    def test_missing_receipt_rejected(self):
        block,receipts=self.rpc_objects()
        with patch.object(chain,'rpc_batch',side_effect=[['0x1',block],[None,*receipts[1:]]]),self.assertRaises(APIError):chain.investigate({'mode':'live'})
    def test_wrong_canonical_member_rejected(self):
        block,receipts=self.rpc_objects();tx=copy.deepcopy(block['transactions'][0]);block['transactions'][0]['hash']='0x'+'ff'*32
        with self.assertRaises(APIError):chain.normalize_snapshot('0x1',block,block,[tx],[receipts[0]],'transaction',tx['hash'],self.at,0)
    def test_concurrent_collection_rejected(self):
        chain.LOCK.acquire()
        try:
            with self.assertRaises(APIError) as ctx:chain.investigate({'mode':'live'})
            self.assertEqual(ctx.exception.status,429)
        finally:chain.LOCK.release()
    def test_contract_creation_not_approval(self):
        self.raw['transactions'][2]['to']=None
        r=chain.build_receipt(self.raw,'demo')
        self.assertNotIn('MAX_APPROVAL',[f['rule_id'] for f in r['output']['findings']])

if __name__=='__main__':unittest.main()
