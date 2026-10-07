"""Bounded Ethereum read-only investigation and deterministic evidence replay."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .catalog import catalog, FORMULAS, LIMITATIONS
from .errors import APIError
from . import decoder, trace as trace_mod, fundflow as fundflow_mod, llm as llm_mod

RPC_URL = 'https://ethereum-rpc.publicnode.com'
RPC_NAME = 'Ethereum PublicNode public RPC'
SCHEMA = 'jiangcheng-chain-investigation/1'
SAMPLE_LIMIT = 6
MAX_RESPONSE = 4 * 1024 * 1024
MAX_RECEIPT = 500 * 1024
MAX_INPUT_BYTES = 16384
MAX_CHECKPOINT_AGE = 1800
LARGE_VALUE = 100 * 10**18
UINT256_MAX = 2**256 - 1
HASH = re.compile(r'^0x[0-9a-fA-F]{64}$')
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
QUANTITY = re.compile(r'^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$')
METHODS = {'eth_chainId','eth_getBlockByNumber','eth_getTransactionByHash','eth_getTransactionReceipt'}
LOCK = threading.Lock()
CACHE = {}
TX_KEYS = {'hash','blockHash','blockNumber','transactionIndex','from','to','value','input','gas','type'}
RC_KEYS = {'transactionHash','blockHash','blockNumber','transactionIndex','status','gasUsed','effectiveGasPrice','blobGasUsed','blobGasPrice'}
RC_OPTIONAL = {'logs'}
RAW_KEYS = {'chain_id','selection','requested_hash','block','finalized','transactions','receipts','collected_at','collection_duration_ms'}
RAW_OPTIONAL = {'trace_calls'}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def stamp(at):
    return datetime.fromtimestamp(at,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def quantity(value, name):
    if not isinstance(value,str) or len(value)>66 or not QUANTITY.fullmatch(value):
        raise APIError(f'{name} 不是有效的 Ethereum 数量字段',502)
    return int(value,16)


def hash_value(value,name):
    if not isinstance(value,str) or not HASH.fullmatch(value):
        raise APIError(f'{name} 不是 32 字节摘要',502)
    return value.lower()


def address(value,name,nullable=False):
    if value is None and nullable:return None
    if not isinstance(value,str) or not ADDRESS.fullmatch(value):raise APIError(f'{name} 地址无效',502)
    return value.lower()


def integer(value,name,minimum=0,maximum=2**53-1):
    if type(value) is not int or not minimum<=value<=maximum:raise APIError(f'{name} 范围无效',502)
    return value


def eth(wei):
    whole,fraction=divmod(wei,10**18)
    return str(whole) if not fraction else f'{whole}.{fraction:018d}'.rstrip('0')


def percent(numerator,denominator):
    if denominator<=0 or not 0<=numerator<=denominator:return None
    hundredths=(numerator*10000+denominator//2)//denominator
    return f'{hundredths//100}.{hundredths%100:02d}'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise APIError('RPC 重定向不在许可范围内，读取已停止',502)


def rpc_batch(calls):
    if not 1<=len(calls)<=8 or any(method not in METHODS for method,_ in calls):raise APIError('读取范围超限')
    body=[{'jsonrpc':'2.0','id':i+1,'method':method,'params':params} for i,(method,params) in enumerate(calls)]
    request=Request(RPC_URL,data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json','User-Agent':'JiangchengChainInvestigator/1.0'})
    try:
        with build_opener(NoRedirect()).open(request,timeout=10) as response:raw=response.read(MAX_RESPONSE+1)
        if len(raw)>MAX_RESPONSE:raise APIError('RPC 响应超过 4 MiB，未生成结论',502)
        result=json.loads(raw)
        if not isinstance(result,list) or len(result)!=len(calls):raise ValueError('batch shape')
        keyed={}
        for row in result:
            if not isinstance(row,dict) or row.get('jsonrpc')!='2.0' or type(row.get('id')) is not int or row['id'] in keyed or 'error' in row or 'result' not in row:raise ValueError('RPC batch')
            keyed[row['id']]=row['result']
        if set(keyed)!=set(range(1,len(calls)+1)):raise ValueError('RPC missing response')
        return [keyed[i+1] for i in range(len(calls))]
    except APIError:raise
    except (HTTPError,URLError,OSError,TimeoutError,ValueError,RecursionError) as exc:
        raise APIError('公共 RPC 暂时不可用或返回不完整；未生成实时结论。可重试或选择独立离线示例。',502) from exc


def _single_rpc(method, params):
    body=json.dumps([{'jsonrpc':'2.0','id':1,'method':method,'params':params}]).encode()
    request=Request(RPC_URL,data=body,method='POST',headers={'Content-Type':'application/json','User-Agent':'JiangchengChainInvestigator/1.0'})
    try:
        with build_opener(NoRedirect()).open(request,timeout=10) as response:raw=response.read(MAX_RESPONSE+1)
        rows=json.loads(raw)
        row=rows[0] if isinstance(rows,list) and rows else {}
        if 'error' in row:return None,row['error']
        return row.get('result'),None
    except (HTTPError,URLError,OSError,TimeoutError,ValueError,RecursionError):
        return None,{'message':'transport error'}


def fetch_trace(txhash):
    """Best-effort internal-call trace for a single tx. Returns {txhash: frames} or {}.
    Tries trace_transaction then debug_traceTransaction; degrades cleanly if unsupported."""
    for method,params in [('trace_transaction',[txhash]),
                          ('debug_traceTransaction',[txhash,{'tracer':'callTracer','timeout':'5000'}])]:
        result,err=_single_rpc(method,params)
        if err is not None and trace_mod.unsupported(err):
            continue
        if result is None:
            continue
        frames=trace_mod.normalize_trace(method,result)
        if frames:
            return {txhash:frames}
    return {}


def block_metadata(block):
    if not isinstance(block,dict):raise APIError('区块缺失；交易可能尚未确认',502)
    return {'number':quantity(block.get('number'),'block.number'),'hash':hash_value(block.get('hash'),'block.hash'),'timestamp':quantity(block.get('timestamp'),'block.timestamp')}


def normalize_tx(tx):
    if not isinstance(tx,dict):raise APIError('交易未找到或尚未上链',502)
    if tx.get('blockNumber') is None or tx.get('blockHash') is None:raise APIError('交易仍处于 pending 状态，不作已确认调查',502)
    out={k:hash_value(tx.get(k),k) for k in ['hash','blockHash']}
    for k in ['blockNumber','transactionIndex','value','gas','type']:out[k]=hex(quantity(tx.get(k),k))
    out['from']=address(tx.get('from'),'from');out['to']=address(tx.get('to'),'to',True)
    data=tx.get('input')
    if not isinstance(data,str) or len(data)>MAX_INPUT_BYTES*2+2 or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*',data):raise APIError('交易调用数据缺失或超过 16 KiB',502)
    out['input']=data.lower()
    return out


def normalize_log(log):
    if not isinstance(log,dict):raise APIError('日志条目缺失',502)
    out={'address':address(log.get('address'),'log.address')}
    topics=log.get('topics')
    if not isinstance(topics,list) or not 1<=len(topics)<=4:raise APIError('日志 topics 形状无效',502)
    out['topics']=[hash_value(t,'log.topic') for t in topics]
    data=log.get('data')
    if not isinstance(data,str) or len(data)>8194 or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*',data):raise APIError('日志 data 无效或过大',502)
    out['data']=data.lower()
    out['logIndex']=hex(quantity(log.get('logIndex'),'logIndex'))
    if log.get('removed') is not None:out['removed']=bool(log['removed'])
    return out


def normalize_receipt(receipt):
    if not isinstance(receipt,dict):raise APIError('交易回执尚未生成，不作已确认调查',502)
    out={k:hash_value(receipt.get(k),k) for k in ['transactionHash','blockHash']}
    for k in ['blockNumber','transactionIndex','status','gasUsed','effectiveGasPrice']:out[k]=hex(quantity(receipt.get(k),k))
    for k in ['blobGasUsed','blobGasPrice']:out[k]=None if receipt.get(k) is None else hex(quantity(receipt[k],k))
    if receipt.get('logs') is not None:
        logs=receipt['logs']
        if not isinstance(logs,list) or len(logs)>200:raise APIError('回执日志数量超限',502)
        out['logs']=[normalize_log(l) for l in logs]
    return out


def normalize_snapshot(chain_id,block,finalized,txs,receipts,selection,requested_hash,collected_at,duration_ms,trace_calls=None):
    if quantity(chain_id,'chainId')!=1:raise APIError('不是 Ethereum Mainnet chainId 1',502)
    metadata=block_metadata(block);checkpoint=block_metadata(finalized)
    members=block.get('transactions')
    if not isinstance(members,list) or len(members)>100000:raise APIError('区块交易成员表无效',502)
    normalized=[normalize_tx(tx) for tx in txs]
    selected=[]
    for tx in normalized:
        index=quantity(tx['transactionIndex'],'transactionIndex')
        if index>=len(members):raise APIError('交易索引超出区块成员表',502)
        member=members[index];member=member.get('hash') if isinstance(member,dict) else member
        if hash_value(member,'block member')!=tx['hash']:raise APIError('交易不是相应规范区块的对应成员',502)
        selected.append(tx['hash'])
    metadata.update(transaction_count=len(members),selected_transaction_hashes=selected)
    out={'chain_id':1,'selection':selection,'requested_hash':requested_hash,'block':metadata,'finalized':checkpoint,'transactions':normalized,'receipts':[normalize_receipt(r) for r in receipts],'collected_at':collected_at,'collection_duration_ms':duration_ms}
    if trace_calls:out['trace_calls']=trace_calls
    return out


def validate(snapshot,mode):
    if not isinstance(snapshot,dict) or not RAW_KEYS<=set(snapshot) or set(snapshot)-RAW_KEYS-RAW_OPTIONAL:raise APIError('快照结构不匹配',502)
    if mode not in {'live','demo','case'} or snapshot['chain_id']!=1 or type(snapshot['chain_id']) is not int:raise APIError('网络身份或模式无效',502)
    collected=integer(snapshot['collected_at'],'collected_at',1,253402300799)
    integer(snapshot['collection_duration_ms'],'collection duration',0,120000)
    selection=snapshot['selection'];requested=snapshot['requested_hash']
    if mode=='demo':
        if selection!='synthetic' or requested is not None:raise APIError('示例来源必须明确为 synthetic',502)
    elif selection not in {'transaction','finalized_sample'}:raise APIError('采样方式无效',502)
    if selection=='transaction':hash_value(requested,'requested_hash')
    elif requested is not None:raise APIError('未指定交易模式不能携带交易摘要',502)
    block,finalized=snapshot['block'],snapshot['finalized']
    if not isinstance(block,dict) or set(block)!={'hash','number','timestamp','transaction_count','selected_transaction_hashes'} or not isinstance(finalized,dict) or set(finalized)!={'hash','number','timestamp'}:raise APIError('区块元数据无效',502)
    for obj in [block,finalized]:
        hash_value(obj['hash'],'block.hash');integer(obj['number'],'block.number');integer(obj['timestamp'],'block.timestamp',1,253402300799)
    integer(block['transaction_count'],'transaction_count',0,100000)
    age=collected-finalized['timestamp']
    if not -30<=age<=MAX_CHECKPOINT_AGE:raise APIError('当前 finalized 检查点超过 30 分钟采集窗口；不能确认为新采集',502)
    if block['number']>finalized['number']:raise APIError('该交易所在区块尚未达到 finalized 高度',502)
    if block['timestamp']>finalized['timestamp']:raise APIError('交易时间晚于 finalized 检查点',502)
    if block['number']==finalized['number'] and (block['hash']!=finalized['hash'] or block['timestamp']!=finalized['timestamp']):raise APIError('同高度区块与 finalized 检查点不一致',502)
    if selection=='finalized_sample' and block['number']!=finalized['number']:raise APIError('默认样本必须来自当前 finalized 区块',502)
    txs,receipts=snapshot['transactions'],snapshot['receipts']
    if not isinstance(txs,list) or not isinstance(receipts,list) or len(txs)!=len(receipts) or len(txs)>SAMPLE_LIMIT:raise APIError('交易/回执样本数量不匹配或超限',502)
    if selection=='transaction' and (len(txs)!=1 or txs[0].get('hash')!=requested):raise APIError('指定交易摘要与证据不一致',502)
    if block['transaction_count']<len(txs):raise APIError('样本数超过区块交易数',502)
    if block['selected_transaction_hashes']!=[t.get('hash') for t in txs if isinstance(t,dict)]:raise APIError('区块成员摘要不一致',502)
    if selection=='finalized_sample' and len(txs)!=min(SAMPLE_LIMIT,block['transaction_count']):raise APIError('默认样本不是区块前六笔',502)
    seen=set();previous=-1
    for index,(tx,receipt) in enumerate(zip(txs,receipts)):
        if not isinstance(tx,dict) or set(tx)!=TX_KEYS or normalize_tx(tx)!=tx or not isinstance(receipt,dict) or (set(receipt)!=RC_KEYS and set(receipt)!=RC_KEYS|RC_OPTIONAL) or normalize_receipt(receipt)!=receipt:raise APIError('快照交易或回执不符合规范字段',502)
        txhash=tx['hash'];txindex=quantity(tx['transactionIndex'],'transactionIndex')
        if txhash in seen or txindex<=previous or txindex>=block['transaction_count']:raise APIError('交易重复或索引顺序无效',502)
        seen.add(txhash);previous=txindex
        if selection=='finalized_sample' and txindex!=index:raise APIError('默认样本索引不是从零开始的前六笔',502)
        if tx['blockHash']!=block['hash'] or receipt['blockHash']!=block['hash'] or receipt['transactionHash']!=txhash or quantity(tx['blockNumber'],'tx.blockNumber')!=block['number'] or quantity(receipt['blockNumber'],'receipt.blockNumber')!=block['number'] or receipt['transactionIndex']!=tx['transactionIndex']:raise APIError('交易、回执、区块摘要/高度/索引关联不一致',502)
        if quantity(receipt['status'],'status') not in {0,1}:raise APIError('回执状态不在 0/1 范围',502)
        gas,used=quantity(tx['gas'],'gas'),quantity(receipt['gasUsed'],'gasUsed')
        if gas<=0 or used>gas:raise APIError('Gas 分母为零或用量超过交易限额',502)
        txtype=quantity(tx['type'],'type')
        if txtype not in {0,1,2,3,4}:raise APIError('暂不支持该交易类型',502)
        if (receipt['blobGasUsed'] is None)!=(receipt['blobGasPrice'] is None):raise APIError('Blob 费用字段缺项',502)
        if txtype==3 and receipt['blobGasUsed'] is None:raise APIError('Blob 交易缺实际 Blob 费用',502)
        if txtype!=3 and receipt['blobGasUsed'] is not None and quantity(receipt['blobGasUsed'],'blobGasUsed')!=0:raise APIError('非 Blob 交易出现 Blob 用量',502)
    # Optional internal-trace frames: txhash -> bounded flat frame list.
    traces=snapshot.get('trace_calls')
    if traces is not None:
        if not isinstance(traces,dict) or len(traces)>len(txs):raise APIError('trace_calls 形状无效',502)
        for th,frames in traces.items():
            hash_value(th,'trace txhash')
            if th not in {t['hash'] for t in txs}:raise APIError('trace 摘要不属于样本交易',502)
            if not isinstance(frames,list) or len(frames)>200:raise APIError('trace 帧数量超限',502)
            for fr in frames:
                if not isinstance(fr,dict) or {'depth','from','to','value_wei','call_type','success'}-set(fr):raise APIError('trace 帧字段不全',502)
    return [
        {'id':'chain','label':'网络身份','passed':True,'observed':'Ethereum Mainnet · chainId 1'},
        {'id':'association','label':'交易与回执关联','passed':True,'observed':f'{len(txs)} 笔摘要、高度、索引与区块成员对应一致'},
        {'id':'finality','label':'确认检查点','passed':True,'observed':f'交易区块 {block["number"]} ≤ finalized {finalized["number"]}；检查点年龄 {age} 秒'},
        {'id':'quantities','label':'量化字段','passed':True,'observed':'状态、整数费用、Gas 分母与 Blob 字段通过'},
    ]


def approval(tx):
    data=tx['input']
    if tx['to'] is None or not re.fullmatch(r'0x095ea7b3[0-9a-f]{128}',data) or data[10:34]!='0'*24:return None
    return {'spender':'0x'+data[34:74],'allowance':int(data[74:],16)}


def quantify(snapshot,mode):
    rows,findings=[],[]
    for tx,r in zip(snapshot['transactions'],snapshot['receipts']):
        status=quantity(r['status'],'status');value=quantity(tx['value'],'value');used=quantity(r['gasUsed'],'gasUsed');gas=quantity(tx['gas'],'gas');price=quantity(r['effectiveGasPrice'],'effectiveGasPrice')
        execution=used*price;blob=0 if r['blobGasUsed'] is None else quantity(r['blobGasUsed'],'blobGasUsed')*quantity(r['blobGasPrice'],'blobGasPrice')
        row={'hash':tx['hash'],'from':tx['from'],'to':tx['to'],'status':status,'block_number':snapshot['block']['number'],'block_hash':snapshot['block']['hash'],'transaction_index':quantity(tx['transactionIndex'],'transactionIndex'),'value_wei':str(value),'value_eth':eth(value),'settled_outer_value_wei':str(value if status else 0),'settled_outer_value_eth':eth(value if status else 0),'gas_limit':str(gas),'gas_used':str(used),'effective_gas_price_wei':str(price),'execution_fee_wei':str(execution),'blob_fee_wei':str(blob),'actual_fee_wei':str(execution+blob),'actual_fee_eth':eth(execution+blob),'gas_use_ratio_percent':percent(used,gas),'selector':tx['input'][:10] if len(tx['input'])>=10 else None,'input':tx['input'],'explorer_url':f'https://etherscan.io/tx/{tx["hash"]}' if mode=='live' else None}
        rows.append(row)
        def finding(rule,label,observation,fields,limitation):
            findings.append({'id':f'{tx["hash"]}:{rule}','rule_id':rule,'transaction_hash':tx['hash'],'label':label,'severity':'review','observation':observation,'evidence_fields':fields,'limitation':limitation})
        if status==0:finding('FAILED','执行失败',f'status=0；实际费用 {row["actual_fee_eth"]} ETH；成功外层转移值 0 ETH',['receipt.status','receipt.gasUsed','receipt.effectiveGasPrice'],'失败原因需 trace/revert 数据，不能仅凭状态认定攻击。')
        if value>=LARGE_VALUE:finding('LARGE_VALUE','大额尝试值',f'外层 value {eth(value)} ETH ≥ 100 ETH',['transaction.value','receipt.status'],'100 ETH 是本产品演示阈值；失败交易不会完成该外层转移，成功也不等于账户净流出。')
        if used*100>=gas*95:finding('HIGH_GAS','较高 Gas 使用比例',f'gasUsed / gasLimit = {row["gas_use_ratio_percent"]}% ≥ 95%',['receipt.gasUsed','transaction.gas'],'95% 是观察阈值；高使用比例不能证明 gas 不足或恶意。')
        decoded=approval(tx)
        if decoded and decoded['allowance']==UINT256_MAX:finding('MAX_APPROVAL','最大额度授权调用形状',f'approve ABI 形状；额度 2²⁵⁶−1；spender {decoded["spender"]}',['transaction.to','transaction.input','receipt.status'],'未核验目标为 ERC-20，也未读取 allowance；失败调用不代表授权生效。')
    summary={'sample_count':len(rows),'success_count':sum(r['status']==1 for r in rows),'failed_count':sum(r['status']==0 for r in rows),'finding_count':len(findings),'attempted_value_eth':eth(sum(int(r['value_wei']) for r in rows)),'settled_outer_value_eth':eth(sum(int(r['settled_outer_value_wei']) for r in rows)),'actual_fee_eth':eth(sum(int(r['actual_fee_wei']) for r in rows))}
    return rows,findings,summary


def explain(snapshot,rows,findings):
    hypotheses=[]
    for row in rows:
        matching=[f for f in findings if f['transaction_hash']==row['hash']]
        if not matching:continue
        alternatives=[];next_checks=[]
        rules={f['rule_id'] for f in matching}
        if 'FAILED' in rules:
            alternatives+=['合约业务条件触发 revert。','可能触及执行资源限制；仅凭 status 和 Gas 比例无法区分。']
            next_checks+=['核对合约已验证源码、调用参数和当时状态。','用有权限的执行 trace 或 revert reason 区分原因；当前未调用 trace。']
        if 'HIGH_GAS' in rules:
            alternatives+=['调用按预期消耗了大部分预估 Gas。','资源不足是备选解释，仍需执行 trace；高比例本身不构成结论。']
            next_checks+=['对照 gasLimit 的设置、同类成功交易和执行 trace。']
        if 'LARGE_VALUE' in rules:
            alternatives+=['可能是正常的大额结算或资金归集。','也可能是需要复核的意外转移；未证明真实主体和意图。']
            next_checks+=['结合授权的业务凭证核对交易意图与地址归属。','核对内部调用、退款及代币变化后再计算净资金流。']
        if 'MAX_APPROVAL' in rules:
            alternatives+=['可能是用户预期的长期额度授权。','选择器也可能属于非 ERC-20 合约，或调用未产生预期授权。']
            next_checks+=['确认合约源码、实际 allowance 与历史授权，再判断剩余暴露。']
        hypotheses.append({'id':'HYP-'+row['hash'],'transaction_hash':row['hash'],'title':'待验证解释 · '+(' / '.join(f['label'] for f in matching)),'facts':[f['observation'] for f in matching],'possible_explanations':list(dict.fromkeys(alternatives)),'next_checks':list(dict.fromkeys(next_checks)),'conclusion_status':'hypotheses_not_causal_conclusions'})
    timeline=[{'id':'tx-'+r['hash'],'time':stamp(snapshot['block']['timestamp']),'kind':'transaction','title':f'区块内第 {r["transaction_index"]+1} 笔 · '+('成功' if r['status'] else '失败'),'detail':f'区块 {r["block_number"]}；费用 {r["actual_fee_eth"]} ETH。区块时间不能区分同块交易秒级先后。','transaction_hash':r['hash']} for r in rows]
    timeline += [{'id':'checkpoint','time':stamp(snapshot['finalized']['timestamp']),'kind':'checkpoint','title':'RPC 返回的 finalized 检查点','detail':f'高度 {snapshot["finalized"]["number"]}；这是检查点区块时间，不是达到最终确认的精确时刻。','transaction_hash':None},{'id':'collection','time':stamp(snapshot['collected_at']),'kind':'collection','title':'本次证据采集','detail':'采集时间与交易发生时间独立记录。','transaction_hash':None}]
    return hypotheses,timeline


def enrich(snapshot, rows, findings):
    """Deep, evidence-only enrichment: decode ERC-20 logs, attach labels, aggregate
    internal calls + token transfers into a bounded fund-flow table, and ask the LLM
    reasoning officer (or deterministic fallback) to rank hypotheses. Pure given the
    snapshot's stored logs/trace frames — no network here."""
    # 1) decode receipt logs per transaction
    events = []
    row_by_hash = {r['hash']: r for r in rows}
    decoded_per_tx = {}
    for rc in snapshot['receipts']:
        if 'logs' not in rc:
            continue
        evs = decoder.decode_receipt_logs(rc)
        decoded_per_tx[rc['transactionHash']] = evs
        if rc['transactionHash'] in row_by_hash:
            row_by_hash[rc['transactionHash']]['decoded_events'] = evs
        for ev in evs:
            ev2 = dict(ev); ev2['_tx_hash'] = rc['transactionHash']; events.append(ev2)
    # 2) trace frames
    traces = snapshot.get('trace_calls') or {}
    flat_frames = []
    for th, frames in traces.items():
        for fr in frames:
            f2 = dict(fr); f2['_tx_hash'] = th; flat_frames.append(f2)
    trace_summary = trace_mod.summarize_frames(flat_frames) if flat_frames else {'frame_count': 0, 'internal_eth_calls': 0, 'total_internal_value_wei': '0', 'failed_frames': 0, 'note': 'RPC 未提供 trace 方法或未采集；已干净降级。'}
    if traces:
        trace_summary['method'] = 'trace_transaction/debug_traceTransaction'
    # 3) address labels
    label_set = {}
    def _lab(addr):
        if addr and addr not in label_set:
            lab = decoder.label_address(addr)
            if lab: label_set[addr] = lab
    for r in rows:
        _lab(r['from']); _lab(r['to'])
    for ev in events:
        _lab(ev.get('from')); _lab(ev.get('to')); _lab(ev.get('contract')); _lab(ev.get('spender'))
    for fr in flat_frames:
        _lab(fr.get('from')); _lab(fr.get('to'))
    address_labels = list(label_set.values())
    # 4) fund flow
    outer = [{'from': r['from'], 'to': r['to'], 'value_wei': r['value_wei'], 'status': r['status']} for r in rows]
    flows = fundflow_mod.aggregate(events, flat_frames, outer)
    # 5) LLM reasoning officer (deterministic fallback when unconfigured)
    evidence_packet = {'summary': {'sample_count': len(rows), 'finding_count': len(findings)},
                      'sampled_transactions': rows, 'findings': findings,
                      'events': events, 'trace_summary': trace_summary,
                      'fund_flow': flows, 'address_labels': address_labels}
    try:
        llm_out = llm_mod.reason(evidence_packet)
    except Exception:
        llm_out = llm_mod.deterministic_fallback(evidence_packet)
    return {'events': events, 'decoded_event_count': len(events),
            'trace_frames': flat_frames[:200], 'trace_summary': trace_summary,
            'address_labels': address_labels, 'fund_flow': flows,
            'reasoning_officer': llm_out}


def build_receipt(snapshot,mode):
    checks=validate(snapshot,mode)
    rows,findings,summary=quantify(snapshot,mode)
    hypotheses,timeline=explain(snapshot,rows,findings)
    deep=enrich(snapshot,rows,findings)
    block,finalized=snapshot['block'],snapshot['finalized']
    _live = mode=='live'
    _case = mode=='case'
    source={'rpc':RPC_URL if _live else None,'name':RPC_NAME if _live else ('内置真实主网案例 · 离线回放（证据包 tests/fixtures）' if _case else '固定合成示例 · 非真实链上交易'),'chain_id':1,'block_number':block['number'],'block_hash':block['hash'],'block_timestamp':block['timestamp'],'transaction_time':stamp(block['timestamp']),'collected_at':stamp(snapshot['collected_at']),'block_age_seconds':snapshot['collected_at']-block['timestamp'],'historical_transaction':block['number']<finalized['number'],'finalized_block_number':finalized['number'],'finalized_block_hash':finalized['hash'],'collection_duration_ms':snapshot['collection_duration_ms'],'explorer_url':f'https://etherscan.io/block/{block["number"]}' if (_live or _case) else None}
    input_data={'mode':mode,'selection':snapshot['selection'],'transaction_hash':snapshot['requested_hash'],'sample_limit':SAMPLE_LIMIT}
    output={'source':source,'summary':summary,'sampled_transactions':rows,'findings':findings,'hypotheses':hypotheses,'timeline':timeline,'deep':deep,'interpretation':{'method':'deterministic_evidence_rules + llm_reasoning_officer (or deterministic fallback)','attribution':'仅披露 from/to 字段，不推断真实主体、控制人或攻击归因。','scope':'单区块有限样本或单笔交易；跨块资金追踪仅在本批证据内聚合。'}}
    stages=[('collect',input_data,snapshot),('validate',snapshot,checks),('quantify',{'transactions':snapshot['transactions'],'receipts':snapshot['receipts']},{'rows':rows,'findings':findings,'summary':summary}),('explain',{'rows':rows,'findings':findings},{'hypotheses':hypotheses,'timeline':timeline}),('report',{'input':input_data,'output':output,'checks':checks},{'input_hash':digest(input_data),'output_hash':digest(output)})]
    agents=catalog()['agents']
    trace=[{'stage_id':stage,'agent_id':agent['id'],'executor':'deterministic_python_tool','used_skill_ids':agent['skills'],'input_hash':digest(before),'output_hash':digest(after)} for (stage,before,after),agent in zip(stages,agents)]
    receipt={'schema':SCHEMA,'mode':mode,'issued_at':stamp(snapshot['collected_at']),'input':input_data,'output':output,'checks':checks,'execution_trace':trace,'raw_snapshot':copy.deepcopy(snapshot),'formulas':copy.deepcopy(FORMULAS),'limitations':LIMITATIONS+(['此回执来自合成示例，摘要与地址不可作为链上事实。'] if mode=='demo' else []),'authenticity':{'signed':False,'on_chain':False,'cross_source_verified':False}}
    receipt['receipt_hash']=digest(receipt)
    if len(json.dumps(receipt,ensure_ascii=False,indent=2).encode())>MAX_RECEIPT:raise APIError('证据超过 500 KiB 导出复核上限，未生成回执',502)
    return receipt


def demo_fixture(at):
    bh='0x'+'ab'*32;block={'hash':bh,'number':'0x123','timestamp':hex(at-600),'transactions':[]};receipts=[]
    for i in range(3):
        txhash='0x'+f'{i+1:064x}';gas=100000 if i!=2 else 65000
        tx={'hash':txhash,'blockHash':bh,'blockNumber':'0x123','transactionIndex':hex(i),'from':'0x'+'11'*20,'to':'0x'+'22'*20,'value':hex((150 if i==0 else 120 if i==1 else 0)*10**18),'input':'0x095ea7b3'+'0'*24+'33'*20+'f'*64 if i==2 else '0x','gas':hex(gas),'type':'0x2'}
        block['transactions'].append(tx)
        receipts.append({'transactionHash':txhash,'blockHash':bh,'blockNumber':'0x123','transactionIndex':hex(i),'status':'0x0' if i==0 else '0x1','gasUsed':hex(100000 if i==0 else 21000 if i==1 else 45000),'effectiveGasPrice':hex(1000000001)})
    return normalize_snapshot('0x1',block,block,block['transactions'],receipts,'synthetic',None,at,0)


def load_case_fixture():
    """Load the bundled real on-chain case snapshot (offline, no network)."""
    import os
    here=os.path.dirname(os.path.abspath(__file__))
    path=os.path.join(here,'..','tests','fixtures','real_swap_tx.json')
    with open(path,encoding='utf-8') as f:return json.load(f)


def investigate(data):
    if not isinstance(data,dict) or set(data)-{'mode','transaction_hash'} or not isinstance(data.get('mode'),str) or data.get('mode') not in {'live','demo','case'}:raise APIError('仅支持 mode=live/demo/case 和可选交易哈希，不接受外部 URL 或钱包操作')
    mode=data['mode'];requested=data.get('transaction_hash')
    if 'transaction_hash' in data and (mode!='live' or not isinstance(requested,str) or not HASH.fullmatch(requested)):raise APIError('指定交易需 live 模式和 0x 开头的 64 位十六进制哈希')
    requested=requested.lower() if requested else None
    if mode=='demo':return build_receipt(demo_fixture(int(time.time())),'demo')
    if mode=='case':return build_receipt(load_case_fixture(),'case')
    if not LOCK.acquire(blocking=False):raise APIError('已有链上采集正在运行，请稍后重试',429)
    try:
        key=requested or 'finalized_sample';cached=CACHE.get(key)
        if cached and time.monotonic()-cached[0]<30:return copy.deepcopy(cached[1])
        start=time.monotonic()
        if requested:
            chain_id,finalized,tx,receipt=rpc_batch([('eth_chainId',[]),('eth_getBlockByNumber',['finalized',False]),('eth_getTransactionByHash',[requested]),('eth_getTransactionReceipt',[requested])])
            ntx=normalize_tx(tx)
            if ntx['hash']!=requested:raise APIError('RPC 返回了其他交易，验收拒绝',502)
            block=rpc_batch([('eth_getBlockByNumber',[ntx['blockNumber'],False])])[0]
            txs,receipts,selection=[tx],[receipt],'transaction'
            try:trace_calls=fetch_trace(requested)
            except Exception:trace_calls={}
        else:
            chain_id,block=rpc_batch([('eth_chainId',[]),('eth_getBlockByNumber',['finalized',True])]);finalized=block
            if not isinstance(block,dict) or not isinstance(block.get('transactions'),list):raise APIError('RPC 未返回 finalized 交易列表',502)
            txs=block['transactions'][:SAMPLE_LIMIT]
            hashes=[hash_value(t.get('hash') if isinstance(t,dict) else None,'transaction.hash') for t in txs]
            receipts=rpc_batch([('eth_getTransactionReceipt',[h]) for h in hashes]) if hashes else []
            selection='finalized_sample';trace_calls={}
        snapshot=normalize_snapshot(chain_id,block,finalized,txs,receipts,selection,requested,int(time.time()),round((time.monotonic()-start)*1000),trace_calls or None)
        result=build_receipt(snapshot,'live')
        if len(CACHE)>=16:CACHE.clear()
        CACHE[key]=(time.monotonic(),copy.deepcopy(result))
        return result
    finally:LOCK.release()


def verify(data):
    if not isinstance(data,dict) or set(data)!={'receipt'} or not isinstance(data['receipt'],dict):raise APIError('请提供完整 receipt 对象')
    receipt=data['receipt'];hash_valid=False;replay_valid=False
    try:
        if len(json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False).encode())>MAX_RECEIPT:raise APIError('回执超过 500 KiB 上限',413)
        if receipt.get('schema')!=SCHEMA:raise ValueError('schema')
        payload={k:v for k,v in receipt.items() if k!='receipt_hash'}
        hash_valid=receipt.get('receipt_hash')==digest(payload)
        raw=receipt.get('raw_snapshot')
        if not isinstance(raw,dict) or type(raw.get('collected_at')) is not int or raw['collected_at']>int(time.time())+300:raise ValueError('collection time')
        expected=build_receipt(raw,receipt.get('mode'))
        replay_valid=receipt==expected
    except APIError as exc:
        if exc.status==413:raise
    except (TypeError,ValueError,KeyError,AttributeError,OverflowError,RecursionError):pass
    return {'valid':hash_valid and replay_valid,'receipt_hash_valid':hash_valid,'rules_replay_valid':replay_valid,'authenticity_verified':False,'chain_requeried':False,'persisted':False,'note':'按原采集时间和规范快照重算全部量化、解释、时间线与执行阶段；没有重新查询链，也没有证明快照来源真实或回执已上链。'}
