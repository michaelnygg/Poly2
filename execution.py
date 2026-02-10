"""
Polymarket Execution Client
=============================
Auth flow (per docs.polymarket.com/developers/CLOB/authentication):
  L1: Private key signs EIP-712 struct -> creates/derives API keys
  L2: API key + HMAC signature -> authenticates all requests
  py-clob-client handles ALL signing automatically.

Wallet types:
  signature_type=0: Standard EOA (MetaMask direct, no proxy)
  signature_type=1: Email/Magic wallet (uses Polymarket proxy)
  signature_type=2: Browser wallet via Polymarket proxy
  For types 1 and 2, FUNDER = your Polymarket profile address (polymarket.com/settings)
"""

import os
import json
import time
import logging
import requests
from typing import List, Dict, Optional

logger = logging.getLogger('polybot')

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


class PolyClient:
    def __init__(self, private_key: str = None, signature_type: int = 0,
                 funder: str = None):
        self.private_key = private_key
        self.signature_type = signature_type
        self.funder = funder
        self.clob_client = None
        self.session = requests.Session()
        self.session.headers.update({'Content-Type': 'application/json'})
        if private_key:
            self._init_clob_client()

    def _init_clob_client(self):
        try:
            from py_clob_client.client import ClobClient

            kwargs = {
                'host': CLOB_HOST,
                'key': self.private_key,
                'chain_id': CHAIN_ID,
            }

            if self.signature_type in (1, 2):
                if not self.funder:
                    logger.error(
                        "signature_type=%d requires FUNDER address "
                        "(your Polymarket profile address from polymarket.com/settings)",
                        self.signature_type)
                    return
                kwargs['signature_type'] = self.signature_type
                kwargs['funder'] = self.funder
            else:
                kwargs['signature_type'] = 0

            self.clob_client = ClobClient(**kwargs)

            logger.info("Deriving API credentials from private key (L1 auth)...")
            api_creds = self.clob_client.create_or_derive_api_creds()
            self.clob_client.set_api_creds(api_creds)

            logger.info("CLOB client initialized with L1+L2 auth")
            logger.info("  Signature type: %d", self.signature_type)
            if self.funder:
                logger.info("  Funder: %s...%s", self.funder[:10], self.funder[-6:])
            logger.info("  API Key: %s...", api_creds.api_key[:8])

        except Exception as e:
            logger.error("CLOB client init failed: %s", e)
            logger.error("Common fixes:")
            logger.error("  - Private key: no 0x prefix")
            logger.error("  - Email login: SIGNATURE_TYPE=1, FUNDER=your Polymarket profile address")
            logger.error("  - MetaMask via polymarket.com: SIGNATURE_TYPE=2, FUNDER=profile address")
            logger.error("  - Direct MetaMask/EOA: SIGNATURE_TYPE=0, no funder needed")
            logger.info("Running in SCAN-ONLY mode")
            self.clob_client = None

    # --- READ-ONLY ---

    def fetch_markets(self, limit: int = 200) -> List[Dict]:
        try:
            resp = self.session.get(
                f"{GAMMA_API}/markets",
                params={'limit': limit, 'active': 'true', 'closed': 'false'},
                timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Fetch markets failed: %s", e)
            return []

    def fetch_events(self, limit: int = 50) -> List[Dict]:
        try:
            resp = self.session.get(
                f"{GAMMA_API}/events",
                params={'limit': limit, 'active': 'true', 'closed': 'false'},
                timeout=15)
            resp.raise_for_status()
            events = resp.json()
            markets = []
            for ev in events:
                if ev.get('markets'):
                    markets.extend(ev['markets'])
            return markets
        except Exception as e:
            logger.error("Fetch events failed: %s", e)
            return []

    def parse_market(self, market: Dict) -> Optional[Dict]:
        tokens = market.get('tokens', [])
        raw_outcomes = market.get('outcomes', '[]')
        raw_prices = market.get('outcomePrices', '[]')

        if isinstance(raw_outcomes, str):
            try: raw_outcomes = json.loads(raw_outcomes)
            except: raw_outcomes = []
        if isinstance(raw_prices, str):
            try: raw_prices = json.loads(raw_prices)
            except: raw_prices = []

        yes_prices = [float(p) for p in raw_prices] if raw_prices else []
        outcomes = raw_outcomes if raw_outcomes else []

        token_ids_yes, token_ids_no = [], []
        for token in tokens:
            outcome = token.get('outcome', '')
            tid = token.get('token_id', '')
            if outcome.lower() == 'yes':
                token_ids_yes.append(tid)
            elif outcome.lower() == 'no':
                token_ids_no.append(tid)

        if not yes_prices or len(yes_prices) < 2:
            return None
        if any(p <= 0 or p >= 1 for p in yes_prices):
            return None
        if not outcomes:
            outcomes = [f'Option {i+1}' for i in range(len(yes_prices))]
        while len(token_ids_yes) < len(yes_prices):
            token_ids_yes.append('')
        while len(token_ids_no) < len(yes_prices):
            token_ids_no.append('')

        return {
            'id': market.get('conditionId', market.get('id', '')),
            'question': market.get('question', 'Unknown'),
            'slug': market.get('slug', ''),
            'outcomes': outcomes,
            'yes_prices': yes_prices,
            'no_prices': [1.0 - p for p in yes_prices],
            'token_ids_yes': token_ids_yes,
            'token_ids_no': token_ids_no,
            'volume': float(market.get('volume', 0)),
            'neg_risk': market.get('negRisk', False),
        }

    # --- EXECUTION ---

    def execute_buy_all_yes(self, market: Dict, position_size: float,
                            dry_run: bool = False) -> Dict:
        if not self.clob_client and not dry_run:
            return {'success': False, 'reason': 'No CLOB client (missing private key)'}

        n = len(market['yes_prices'])
        yes_sum = sum(market['yes_prices'])
        per_outcome = position_size / n
        expected_profit = position_size * (1.0 - yes_sum)

        orders = []
        for i in range(n):
            tid = market['token_ids_yes'][i]
            if not tid:
                return {'success': False, 'reason': f'Missing token ID for outcome {i}'}
            orders.append({
                'token_id': tid,
                'outcome': market['outcomes'][i],
                'price': market['yes_prices'][i],
                'amount': per_outcome,
            })

        result = {
            'success': False, 'type': 'buy_all_yes',
            'market': market['question'], 'n_orders': n,
            'orders': orders, 'total_cost': position_size,
            'expected_profit': expected_profit, 'yes_sum': yes_sum,
            'dry_run': dry_run, 'fills': [],
        }

        if dry_run:
            result['success'] = True
            result['reason'] = 'DRY RUN'
            logger.info("DRY RUN: %s | Cost: $%.2f | Profit: $%.4f",
                        market['question'][:50], position_size, expected_profit)
            return result

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            fills = []
            for order in orders:
                try:
                    mo = MarketOrderArgs(
                        token_id=order['token_id'],
                        amount=order['amount'],
                        side=BUY,
                    )
                    signed = self.clob_client.create_market_order(mo)
                    resp = self.clob_client.post_order(signed, OrderType.FOK)
                    fills.append({'outcome': order['outcome'], 'status': 'submitted',
                                  'response': str(resp)})
                    logger.info("  OK %s: $%.2f @ %.3f", order['outcome'],
                                order['amount'], order['price'])
                    time.sleep(0.15)
                except Exception as e:
                    fills.append({'outcome': order['outcome'], 'status': 'failed',
                                  'error': str(e)})
                    logger.error("  FAIL %s: %s", order['outcome'], e)

            result['fills'] = fills
            result['success'] = all(f['status'] == 'submitted' for f in fills)
            result['reason'] = 'All submitted' if result['success'] else 'Partial fill'
        except Exception as e:
            result['reason'] = f'Execution error: {e}'
        return result

    def execute_sell_all_yes(self, market: Dict, position_size: float,
                             dry_run: bool = False) -> Dict:
        if not self.clob_client and not dry_run:
            return {'success': False, 'reason': 'No CLOB client'}

        n = len(market['yes_prices'])
        yes_sum = sum(market['yes_prices'])
        per_outcome = position_size / n
        expected_profit = position_size * (yes_sum - 1.0)

        result = {
            'success': dry_run, 'type': 'sell_all_yes',
            'market': market['question'], 'n_orders': n,
            'total_cost': position_size, 'expected_profit': expected_profit,
            'yes_sum': yes_sum, 'dry_run': dry_run,
            'reason': 'DRY RUN' if dry_run else '',
        }

        if dry_run:
            logger.info("DRY RUN SELL: %s | Profit: $%.4f",
                        market['question'][:50], expected_profit)
            return result

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            fills = []
            for i in range(n):
                tid = market['token_ids_yes'][i]
                if not tid:
                    continue
                try:
                    mo = MarketOrderArgs(token_id=tid, amount=per_outcome, side=SELL)
                    signed = self.clob_client.create_market_order(mo)
                    resp = self.clob_client.post_order(signed, OrderType.FOK)
                    fills.append({'outcome': market['outcomes'][i], 'status': 'submitted'})
                    logger.info("  OK SELL %s: $%.2f", market['outcomes'][i], per_outcome)
                    time.sleep(0.15)
                except Exception as e:
                    fills.append({'outcome': market['outcomes'][i], 'status': 'failed', 'error': str(e)})
                    logger.error("  FAIL SELL %s: %s", market['outcomes'][i], e)

            result['fills'] = fills
            result['success'] = all(f['status'] == 'submitted' for f in fills)
            result['reason'] = 'All submitted' if result['success'] else 'Partial fill'
        except Exception as e:
            result['reason'] = f'Execution error: {e}'
        return result
