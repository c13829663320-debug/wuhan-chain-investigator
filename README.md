# 江城链察（wuhan-chain-investigator）

以太坊链上异动调查 Agent。给定交易哈希（或抽样的 finalized 区块前 6 笔），采集链上证据、计算执行指标、解码 ERC-20 日志 / 内部调用 trace、打地址标签、聚合资金流，再由「调查推理官」产出排序后的假设、跨源核验问题与不确定性标注。它不是费用计算器，而是给出**可核查解释**的调查员。

> 部署目标：<https://wutiantian.cn/eth-investigator/>。本仓库为独立源码与后端，不依赖其他项目。

---

## 1. 环境依赖与启动

- Python 3.9+（后端**仅用标准库**，无第三方依赖）
- Node.js 22.12+、npm

```bash
# 1) 安装前端依赖（首次）
npm ci          # 若 lockfile 与本机平台 native 包不匹配，可退化为 npm install

# 2) 启动后端（端口 8775）
python3 -m server.main --port 8775 --data-dir ./data

# 3) 另开终端启动前端（端口 5202，/api 代理到 8775）
npm run dev
# 访问 http://127.0.0.1:5202/
```

构建与检查：

```bash
npm run typecheck   # tsc --noEmit
npm run build       # tsc + vite build --base=/eth-investigator/
npm test            # python3 -m unittest discover -s tests
```

生产后端：

```bash
PUBLIC_ORIGIN=https://wutiantian.cn python3 -m server.main --port 8775 --data-dir /独立数据目录
```

---

## 2. LLM「调查推理官」配置

推理官通过 **OpenAI 兼容** 的 `chat/completions` 接入。全部从环境变量读取；**不配置 key 时自动降级为确定性规则结论，并在输出中明确标注 `engine=deterministic_fallback`**。测试与构建不依赖任何 key。

| 环境变量 | 含义 | 示例 |
|---|---|---|
| `JIANGCHENG_LLM_BASE_URL` | OpenAI 兼容 base（不带尾斜杠） | `https://api.openai.com/v1` |
| `JIANGCHENG_LLM_API_KEY` | Bearer key | `sk-...` |
| `JIANGCHENG_LLM_MODEL` | 模型名 | `gpt-4o-mini` |
| `JIANGCHENG_LLM_TIMEOUT` | 可选，秒，默认 8 | `8` |

```bash
export JIANGCHENG_LLM_BASE_URL=https://api.openai.com/v1
export JIANGCHENG_LLM_API_KEY=sk-...
export JIANGCHENG_LLM_MODEL=gpt-4o-mini
```

推理官**只吃服务器喂给它的结构化证据包**（指标 + 解码日志 + trace + 标签 + 资金流），被要求输出 JSON：排序后的假设、置信度、支撑证据字段、备选解释、跨源核验问题、不确定项。调用失败/超时即降级，绝不阻塞调查。

---

## 3. 三种调查模式

POST `/api/investigate`：

| mode | 行为 | 联网 |
|---|---|---|
| `demo` | 内置合成示例（3 笔），断网可用 | 否 |
| `case` | **内置真实主网案例**（一笔多跳 DEX swap），离线回放 | 否 |
| `live` | 实时从公共 RPC 采集；可带 `transaction_hash` | 是 |

```bash
# 离线真实案例（推荐演示）
curl -s -X POST http://127.0.0.1:8775/api/investigate \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:5202' \
  -d '{"mode":"case"}'

# 实时调查一笔指定交易
curl -s -X POST http://127.0.0.1:8775/api/investigate \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:5202' \
  -d '{"mode":"live","transaction_hash":"0x...64hex..."}'
```

POST `/api/verify` 上传完整 receipt 对象，按原快照**确定性重放**全部量化/解释/资金流/推理，校验哈希一致性。

---

## 4. 真实案例走读（可复现）

内置 fixture：`tests/fixtures/real_swap_tx.json`

- 交易哈希：`0xcaccdaff3abfc2cd21acefd5869b9a066ea69b488bb212f11463b8d7c1f890e0`
- 区块：`26139095`（Ethereum Mainnet）
- 是什么：一笔聚合路由的多跳 swap，经多个池子完成 **WETH ↔ USDC/LINK/某 altcoin** 兑换。
- 证据包解码出 **14 条 ERC-20 Transfer 日志**、**9 行地址资金流**、自动标注 WETH/USDC/LINK 标签。
- 该公共 RPC（publicnode）不暴露 `trace_*`/`debug_*` → trace 帧为 0，并在 `deep.trace_summary` 明确给出「已干净降级」说明（**这正是要求的优雅降级演示**）。

复现步骤：

```bash
python3 -m unittest tests.test_enrichment.RealCaseTests -v
# 或直接：
python3 -c "from server import chain; r=chain.investigate({'mode':'case'}); \
  print(r['output']['deep']['decoded_event_count'], r['output']['deep']['fund_flow'])"
```

重新抓取一笔新的真实案例（联网）：

```bash
python3 - <<'EOF'
import json, time
from server import chain
cid, fb = chain.rpc_batch([('eth_chainId',[]),('eth_getBlockByNumber',['finalized',True])])
h = fb['transactions'][4]['hash']          # 选一笔日志丰富的
cid, fin, tx, rcpt = chain.rpc_batch([('eth_chainId',[]),('eth_getBlockByNumber',['finalized',False]),
                                      ('eth_getTransactionByHash',[h]),('eth_getTransactionReceipt',[h])])
block = chain.rpc_batch([('eth_getBlockByNumber',[tx['blockNumber'],False])])[0]
trace = chain.fetch_trace(h)
snap = chain.normalize_snapshot(cid, block, fin, [tx], [rcpt], 'transaction', h, int(time.time()), 0, trace or None)
json.dump(snap, open('tests/fixtures/real_swap_tx.json','w'), indent=2, ensure_ascii=False)
print('refreshed', h)
EOF
```

---

## 5. RPC 方法要求与降级说明

| 方法 | 用途 | 不支持时 |
|---|---|---|
| `eth_chainId` / `eth_getBlockByNumber` / `eth_getTransactionByHash` / `eth_getTransactionReceipt` | 基础采集（必选） | 实时模式整体报错，不回退到合成数据 |
| `eth_getTransactionReceipt.logs` | ERC-20 Transfer/Approval 解码 | 无 logs 则该交易 `decoded_events=[]` |
| `trace_transaction` 或 `debug_traceTransaction`(callTracer) | 内部交易 | 返回 method-not-found 时自动试另一个；都不支持则 trace 帧为空并在 `trace_summary.note` 说明 |
| `eth_call`（代币元数据） | 暂用内置精选标签库替代 | 未知代币 symbol 显示为空，不伪造 |

所有读取仍是**有界批量 JSON-RPC**：单笔交易最多一次 trace 尝试，日志 ≤200 条，回执 ≤500 KiB。

---

## 6. 模块清单（本次新增/修改）

后端 `server/`：

- `chain.py` — 主流程；本次扩展：回执可选 `logs`、快照可选 `trace_calls`、`enrich()` 深度解析管线、`fetch_trace()`、`mode=case` 离线案例、`/api/investigate` 集成推理官。
- `decoder.py`（新）— ERC-20 `Transfer`/`Approval` 日志解码（topics/data → from/to/金额/spender，补代币元数据）。
- `trace.py`（新）— `trace_transaction`(Parity) 与 `debug_traceTransaction`(callTracer/Geth) 两种结果归一为扁平帧，方法不支持时干净降级。
- `fundflow.py`（新）— 跨解码日志 + 内部调用 + 外层 value 聚合为每地址净流表。
- `labels.py`（新）— 精选公开协议/代币地址标签库（Uniswap、0x、Seaport、Multicall3、Lido、WETH/USDC/USDT/DAI/WBTC/LINK 等）。
- `llm.py`（新）— OpenAI 兼容 chat/completions 适配层；无 key/失败时确定性降级；可注入 transport 做桩测试。
- `main.py` — 修复 body-limit：返回 413 前先有界 drain 请求体，避免连接被重置。
- `catalog.py` — 原有岗位/公式/局限（保留）。

测试 `tests/`：

- `test_chain.py`（原有 33 项，保持全绿）
- `test_http.py`（原有 10 项，含修复后的 `test_body_limit`）
- `test_enrichment.py`（新 16 项：日志解码已知向量、trace 解析与降级、标签、资金流、LLM 桩、真实案例离线回放）

Fixture：`tests/fixtures/real_swap_tx.json`。

---

## 7. 来源与边界标注

**赛前已有（基线）**：交易/回执/finalized 采集与关联验收、执行 Gas/Blob 费、尝试值 vs 成功外层值、Gas 使用比例、最大额度 approve 形状、确定性事实/备选/待核查结构、证据哈希与离线重放、HTTP 同源与体积限制。

**本次新增**：ERC-20 Transfer/Approval 日志解码、内部 trace 归一与降级、精选地址标签、跨解码证据的资金流聚合、LLM 调查推理官适配层（含确定性降级）、`mode=case` 真实主网离线案例、body-limit 连接干净化修复、对应 unittest。

**第三方来源**：

- 公共 RPC：Ethereum PublicNode `https://ethereum-rpc.publicnode.com`（只读）。
- ERC-20 事件签名常量：`Transfer(address,address,uint256)` / `Approval(address,address,uint256)`（EIP-20 标准）。
- 地址标签为公开协议级标注；**不推断真人/实体身份，不做攻击归因**。
- 赛题依据：汉客松 S1 & ETH Wuhan 2026 选手手册；接口依据：Ethereum JSON-RPC 文档。

**硬约束**：只读、无钱包、不碰私钥/助记词；后端纯标准库；回执未签名未上链，离线重放只证明内部一致性，不证明快照来源真实。
