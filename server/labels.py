"""Curated, read-only address label book and well-known ERC-20 metadata.

This is a small hand-curated registry for mainnet (chainId=1). It is NOT a
complete identity graph: labels are public, protocol-level tags only, and we
never infer a human/entity identity beyond what the protocol itself advertises.
"""
from __future__ import annotations

# address (lowercase 0x + 40 hex) -> human readable public label
KNOWN_ADDRESSES = {
    # --- token contracts (also in TOKENS below) ---
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2': 'WETH9 (Wrapped Ether)',
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48': 'USDC (Circle)',
    '0xdac17f958d2ee523a2206206994597c13d831ec7': 'USDT (Tether)',
    '0x6b175474e89094c44da98b954eedeac495271d0f': 'DAI (Maker)',
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599': 'WBTC (BitGo)',
    '0x514910771af9ca656af840dff83e8264ecf986ca': 'LINK (Chainlink)',
    # --- DeFi / protocol contracts ---
    '0x7a250d5630b4cf539739df2c5dacb4c659f2488d': 'Uniswap V2 Router02',
    '0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f': 'Uniswap V2 Factory',
    '0xe592427a0aece92de3edee1f18e0157c05861564': 'Uniswap V3 SwapRouter (0.99%)',
    '0x1f98431c8ad98523631ae4a59f267346ea31f984': 'Uniswap V3 Factory',
    '0xdef1c0ded9bec7f1a1670819833240f027b25ef36a': '0x Exchange Proxy (Swap)',
    '0x00000000000000adc008c1c500af0ccb0cad9230b': 'OpenSea Seaport 1.5',
    '0xba13e42b98f5009055d220b2e87979d8a7a28e1a': '1inch V5 Aggregator',
    '0xcaf1a2d710a9457f8cb7c4455d78a2e9494f6d81': '1inch V6 Aggregator',
    '0xc2e846d8bf7f1f8c1b4b8a9b5f8e8c7a6d5e4f3a': 'Placeholder Router (unknown)',
    '0xca11bde05977b3631167028862be2a173976ca11': 'Multicall3',
    '0x3e4073eb7856ea39019d15f82d4bb173c7293547': 'Lido Execution Layer Rewards Vault',
    '0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640': 'Uniswap V3 Pool USDC/WETH 0.05%',
}

# ERC-20 token metadata: address -> {symbol, name, decimals}
TOKENS = {
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2': {'symbol': 'WETH', 'name': 'Wrapped Ether', 'decimals': 18},
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48': {'symbol': 'USDC', 'name': 'USD Coin', 'decimals': 6},
    '0xdac17f958d2ee523a2206206994597c13d831ec7': {'symbol': 'USDT', 'name': 'Tether USD', 'decimals': 6},
    '0x6b175474e89094c44da98b954eedeac495271d0f': {'symbol': 'DAI', 'name': 'Dai Stablecoin', 'decimals': 18},
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599': {'symbol': 'WBTC', 'name': 'Wrapped BTC', 'decimals': 8},
    '0x514910771af9ca656af840dff83e8264ecf986ca': {'symbol': 'LINK', 'name': 'Chainlink Token', 'decimals': 18},
}

# addresses known to be high-risk / privacy-preserving mixers (label only, no attribution)
HIGH_RISK = {
    '0x722122df12d4e14e13ac33323e7b81414f66543f': 'Tornado Cash mixer (0.1 ETH instance)',
    '0x12d66f84a4a3a6f5fd570433a6a3b24e4f8f1f4a': 'Tornado Cash-like mixer (community tag)',
}


def _norm(addr):
    if not isinstance(addr, str):
        return None
    a = addr.lower()
    if len(a) == 42 and a.startswith('0x'):
        return a
    return None


def label_of(address):
    """Return a public label dict for a known address, else None. Never invents identity."""
    a = _norm(address)
    if a is None:
        return None
    if a in HIGH_RISK:
        return {'address': a, 'label': HIGH_RISK[a], 'kind': 'mixer_risk'}
    if a in KNOWN_ADDRESSES:
        return {'address': a, 'label': KNOWN_ADDRESSES[a], 'kind': 'protocol' if a not in TOKENS else 'token'}
    return None


def token_meta(address):
    a = _norm(address)
    if a is None or a not in TOKENS:
        return None
    out = dict(TOKENS[a])
    out['address'] = a
    return out


def human_amount(wei_int, decimals):
    """Format a raw uint256 token amount with the token's decimals. Pure, no network."""
    if not isinstance(wei_int, int) or wei_int < 0:
        return None
    if decimals <= 0:
        return str(wei_int)
    whole, frac = divmod(wei_int, 10 ** decimals)
    if frac == 0:
        return str(whole)
    return f'{whole}.{frac:0{decimals}d}'.rstrip('0')
