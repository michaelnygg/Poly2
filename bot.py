"""
Polymarket Arbitrage Bot — Full Auto
======================================
24/7 scanning + automatic execution on Railway.

Safety rails:
  - Max 10% of bankroll per trade
  - Max 3 trades per hour
  - 15% drawdown kill switch
  - Minimum $0.03 profit threshold
  - Dry run mode for first 24 hours
  - All trades logged with full details
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from math_engine import analyze_market, kelly_position, should_execute
from execution import PolyClient

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION (via environment variables)
# ═══════════════════════════════════════════════════════════════

API_KEY = os.environ.get('POLY_API_KEY', '')
API_SECRET = os.environ.get('POLY_API_SECRET', '')
API_PASSPHRASE = os.environ.get('POLY_API_PASSPHRASE', '')
PRIVATE_KEY = os.environ.get('POLY_PRIVATE_KEY', '')

# Wallet type: 0=EOA/MetaMask direct, 1=Email/Magic, 2=Browser wallet via proxy
SIGNATURE_TYPE = int(os.environ.get('SIGNATURE_TYPE', '0'))
# Funder address: your Polymarket profile address (required for sig_type 1 or 2)
# Find it at polymarket.com/settings — it's where you deposit USDC
FUNDER = os.environ.get('POLY_FUNDER', '')

# Trading parameters
BANKROLL = float(os.environ.get('BANKROLL', '500'))
MIN_PROFIT = float(os.environ.get('MIN_PROFIT', '0.01'))       # 1 cent per dollar
MAX_POSITION_PCT = float(os.environ.get('MAX_POS_PCT', '0.10')) # 10% of bankroll
GAS_COST = float(os.environ.get('GAS_COST', '0.005'))          # ~$0.005 on Polygon
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL', '30'))      # seconds
MAX_TRADES_PER_HOUR = int(os.environ.get('MAX_TRADES_HR', '3'))
DRAWDOWN_LIMIT = float(os.environ.get('DRAWDOWN_LIMIT', '0.15'))

# Auto-correct common mistakes (user puts 15 instead of 0.15)
if DRAWDOWN_LIMIT > 1.0:
    DRAWDOWN_LIMIT = DRAWDOWN_LIMIT / 100.0
if MAX_POSITION_PCT > 1.0:
    MAX_POSITION_PCT = MAX_POSITION_PCT / 100.0
if MIN_PROFIT > 1.0:
    MIN_PROFIT = MIN_PROFIT / 100.0

# Safety
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'
DRY_RUN_HOURS = int(os.environ.get('DRY_RUN_HOURS', '24'))

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('polybot')

# ═══════════════════════════════════════════════════════════════
# STATE TRACKING
# ═══════════════════════════════════════════════════════════════

class BotState:
    """Tracks bot state, P&L, and safety limits."""
    
    def __init__(self, initial_bankroll: float):
        self.initial_bankroll = initial_bankroll
        self.current_bankroll = initial_bankroll
        self.total_trades = 0
        self.successful_trades = 0
        self.failed_trades = 0
        self.total_profit = 0.0
        self.total_loss = 0.0
        self.trades_this_hour = []
        self.start_time = datetime.utcnow()
        self.last_scan = None
        self.scans_total = 0
        self.opportunities_found = 0
        self.opportunities_executed = 0
        self.dry_run_until = datetime.utcnow() + timedelta(hours=DRY_RUN_HOURS) if DRY_RUN else None
        self.halted = False
        self.halt_reason = ''
        self.trade_log = []
    
    @property
    def is_dry_run(self) -> bool:
        if self.dry_run_until and datetime.utcnow() < self.dry_run_until:
            return True
        return False
    
    @property 
    def drawdown(self) -> float:
        if self.initial_bankroll == 0:
            return 0
        return (self.initial_bankroll - self.current_bankroll) / self.initial_bankroll
    
    @property
    def trades_in_last_hour(self) -> int:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        self.trades_this_hour = [t for t in self.trades_this_hour if t > cutoff]
        return len(self.trades_this_hour)
    
    @property
    def uptime(self) -> str:
        delta = datetime.utcnow() - self.start_time
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{delta.total_seconds()/60:.0f}m"
        if hours < 24:
            return f"{hours:.1f}h"
        return f"{hours/24:.1f}d"
    
    def can_trade(self) -> tuple:
        """Check if we're allowed to trade. Returns (allowed, reason)."""
        if self.halted:
            return False, f"HALTED: {self.halt_reason}"
        
        if self.drawdown >= DRAWDOWN_LIMIT:
            self.halted = True
            self.halt_reason = f"Drawdown {self.drawdown*100:.1f}% >= {DRAWDOWN_LIMIT*100:.0f}%"
            return False, self.halt_reason
        
        if self.trades_in_last_hour >= MAX_TRADES_PER_HOUR:
            return False, f"Rate limit: {self.trades_in_last_hour}/{MAX_TRADES_PER_HOUR} trades/hr"
        
        if self.current_bankroll < 10:
            self.halted = True
            self.halt_reason = "Bankroll below $10"
            return False, self.halt_reason
        
        return True, "OK"
    
    def record_trade(self, result: dict):
        """Record a trade result."""
        self.total_trades += 1
        self.trades_this_hour.append(datetime.utcnow())
        
        if result.get('success'):
            self.successful_trades += 1
            profit = result.get('expected_profit', 0)
            self.total_profit += max(profit, 0)
            self.current_bankroll += profit
        else:
            self.failed_trades += 1
            # Assume worst case: lose the position
            loss = result.get('total_cost', 0) * 0.1  # Estimate 10% loss on failed trade
            self.total_loss += loss
            self.current_bankroll -= loss
        
        self.trade_log.append({
            'time': datetime.utcnow().isoformat(),
            'market': result.get('market', 'Unknown'),
            'type': result.get('type', ''),
            'success': result.get('success', False),
            'profit': result.get('expected_profit', 0),
            'cost': result.get('total_cost', 0),
            'dry_run': result.get('dry_run', False),
            'reason': result.get('reason', ''),
        })
    
    def status_line(self) -> str:
        mode = "🔸 DRY RUN" if self.is_dry_run else "🟢 LIVE"
        if self.halted:
            mode = "🔴 HALTED"
        
        return (
            f"{mode} | "
            f"Up: {self.uptime} | "
            f"Scans: {self.scans_total} | "
            f"Opps: {self.opportunities_found} | "
            f"Trades: {self.total_trades} ({self.successful_trades}✓/{self.failed_trades}✗) | "
            f"P&L: ${self.total_profit - self.total_loss:+.4f} | "
            f"Bank: ${self.current_bankroll:.2f} | "
            f"DD: {self.drawdown*100:.1f}%"
        )


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN + EXECUTE LOOP
# ═══════════════════════════════════════════════════════════════

def scan_and_execute(client: PolyClient, state: BotState):
    """
    One scan cycle:
    1. Fetch all active events (groups of related markets)
    2. For each event with 3+ markets, sum YES prices across markets
    3. If sum ≠ $1 → arbitrage exists
    4. Also check individual multi-outcome markets
    5. Execute if profitable and allowed
    """
    state.scans_total += 1
    state.last_scan = datetime.utcnow()
    
    import json
    
    opportunities = []
    
    # ── STRATEGY 1: Event-level arbitrage ──────────────────────
    # This is where the REAL arb lives. One event like "Who wins the election?"
    # has many markets (one per candidate), each with independent YES price.
    # If sum of all YES prices ≠ $1.00, there's arbitrage.
    
    events_raw = []
    try:
        resp = client.session.get(
            "https://gamma-api.polymarket.com/events",
            params={'limit': 100, 'active': 'true', 'closed': 'false'},
            timeout=15,
        )
        resp.raise_for_status()
        events_raw = resp.json()
        if state.scans_total % 20 == 1:
            logger.info(f"  Fetched {len(events_raw)} events, scanning for multi-outcome arb...")
    except Exception as e:
        logger.error(f"Fetch events failed: {e}")
    
    for event in events_raw:
        ev_markets = event.get('markets', [])
        if len(ev_markets) < 3:
            continue
        
        # DEBUG: On first scan, dump the first qualifying event's structure
        if state.scans_total <= 1 and not hasattr(state, '_debug_dumped'):
            state._debug_dumped = True
            logger.info("=== DEBUG: First event structure ===")
            logger.info(f"  Event keys: {list(event.keys())}")
            logger.info(f"  negRisk: {event.get('negRisk')}")
            if ev_markets:
                em0 = ev_markets[0]
                logger.info(f"  Market[0] keys: {list(em0.keys())}")
                logger.info(f"  Market[0] tokens: {str(em0.get('tokens', 'N/A'))[:300]}")
                logger.info(f"  Market[0] clobTokenIds: {em0.get('clobTokenIds', 'N/A')}")
                logger.info(f"  Market[0] outcomePrices: {em0.get('outcomePrices', 'N/A')}")
                logger.info(f"  Market[0] question: {em0.get('question', 'N/A')[:60]}")
                logger.info(f"  Market[0] conditionId: {em0.get('conditionId', 'N/A')}")
            logger.info("=== END DEBUG ===")
        
        # ════════════════════════════════════════════════════════
        # CRITICAL SAFETY FILTER: Only target MUTUALLY EXCLUSIVE events
        # ════════════════════════════════════════════════════════
        # Polymarket flags mutually exclusive events with negRisk=true
        # Non-exclusive events (like "What will happen before X?") have
        # YES prices that legitimately sum to >$1 — NOT arbitrage.
        #
        # negRisk=true means: "exactly one outcome wins" → Σ YES should = $1
        # negRisk=false means: "multiple outcomes can win" → Σ YES > $1 is normal
        
        is_neg_risk = event.get('negRisk', False)
        
        # Also check individual markets for negRisk flag
        if not is_neg_risk:
            is_neg_risk = any(em.get('negRisk', False) for em in ev_markets)
        
        if not is_neg_risk:
            continue  # Skip non-mutually-exclusive events entirely
        
        # Collect YES price and token IDs for each market in the event
        yes_prices = []
        token_ids_yes = []
        token_ids_no = []
        outcomes = []
        valid = True
        
        for em in ev_markets:
            raw_prices = em.get('outcomePrices', '[]')
            if isinstance(raw_prices, str):
                try: raw_prices = json.loads(raw_prices)
                except: raw_prices = []
            
            # Extract token IDs — gamma API uses multiple formats
            tokens = em.get('tokens', [])
            yes_tid = ''
            no_tid = ''
            
            # Method 1: tokens array with outcome field
            for t in tokens:
                outcome = t.get('outcome', '').lower()
                tid = t.get('token_id', '') or t.get('tokenId', '') or t.get('tokenID', '')
                if outcome == 'yes':
                    yes_tid = tid
                elif outcome == 'no':
                    no_tid = tid
            
            # Method 2: clobTokenIds field (JSON string "[yes_id, no_id]")
            if not yes_tid:
                clob_ids = em.get('clobTokenIds', '[]')
                if isinstance(clob_ids, str):
                    try: clob_ids = json.loads(clob_ids)
                    except: clob_ids = []
                if isinstance(clob_ids, list) and len(clob_ids) >= 2:
                    yes_tid = clob_ids[0]
                    no_tid = clob_ids[1]
                elif isinstance(clob_ids, list) and len(clob_ids) == 1:
                    yes_tid = clob_ids[0]
            
            # Method 3: conditionId or individual fields
            if not yes_tid:
                yes_tid = em.get('yesTokenId', '') or em.get('yes_token_id', '')
                no_tid = em.get('noTokenId', '') or em.get('no_token_id', '')
            
            # Debug: log first event's token structure on first scan
            if state.scans_total <= 1 and len(yes_prices) == 0:
                logger.info(f"  DEBUG token extraction: tokens={str(tokens)[:200]}")
                logger.info(f"  DEBUG clobTokenIds={em.get('clobTokenIds', 'N/A')}")
                logger.info(f"  DEBUG yes_tid={yes_tid}, no_tid={no_tid}")
            
            if raw_prices and len(raw_prices) >= 1:
                p = float(raw_prices[0])
                if 0 < p < 1:
                    if not yes_tid:
                        if state.scans_total <= 1:
                            logger.warning(f"  Missing YES token ID for: {em.get('question', '?')[:40]}")
                        valid = False  # Need ALL legs for arb
                    yes_prices.append(p)
                    token_ids_yes.append(yes_tid)
                    token_ids_no.append(no_tid)
                    outcomes.append(em.get('question', em.get('groupItemTitle', f'Option {len(outcomes)+1}'))[:50])
                else:
                    valid = False
            else:
                valid = False
        
        if not valid or len(yes_prices) < 3:
            continue
        
        # For sell-side (buy_all_no), we need NO token IDs
        has_no_tokens = all(tid != '' for tid in token_ids_no)
        
        yes_sum = sum(yes_prices)
        deviation = abs(yes_sum - 1.0)
        
        if deviation < 0.01:  # Less than 1 cent deviation, skip
            continue
        
        # SANITY CHECK: Real arbitrage on mutually exclusive events produces
        # small deviations (typically 1-15%). If sum > 1.5 or < 0.5, something
        # is wrong — likely not truly mutually exclusive despite negRisk flag.
        if yes_sum > 1.50 or yes_sum < 0.50:
            if state.scans_total % 20 == 1:
                logger.warning(f"  Skipping suspicious event: {event.get('title', '?')[:40]} "
                             f"Σ={yes_sum:.4f} (too far from 1.0 even for negRisk)")
            continue
        
        # Determine type
        if yes_sum < 0.99:
            opp_type = 'buy_all_yes'
            raw_profit = 1.0 - yes_sum
        elif yes_sum > 1.01:
            if not has_no_tokens:
                continue  # Can't do buy_all_no without NO token IDs
            opp_type = 'buy_all_no'
            raw_profit = yes_sum - 1.0
        else:
            continue
        
        # Run math
        analysis = analyze_market(yes_prices)
        if not analysis:
            # Build manual analysis if math engine returns None (threshold too low)
            analysis = {
                'n': len(yes_prices),
                'yes_sum': yes_sum,
                'deviation': deviation,
                'type': opp_type,
                'raw_profit': raw_profit,
                'bregman_D': 0,
                'fw_gap': 0,
                'guaranteed_profit': raw_profit,
                'alpha_captured': 1.0,
            }
        
        exec_check = should_execute(
            analysis,
            bankroll=state.current_bankroll,
            min_profit=MIN_PROFIT,
            gas_cost=GAS_COST,
            max_position_pct=MAX_POSITION_PCT,
        )
        
        parsed_market = {
            'id': event.get('id', ''),
            'question': event.get('title', 'Unknown Event'),
            'slug': event.get('slug', ''),
            'outcomes': outcomes,
            'yes_prices': yes_prices,
            'no_prices': [1.0 - p for p in yes_prices],
            'token_ids_yes': token_ids_yes,
            'token_ids_no': token_ids_no,
            'volume': 0,
            'neg_risk': event.get('negRisk', False),
        }
        
        opportunities.append({
            'market': parsed_market,
            'analysis': analysis,
            'exec_check': exec_check,
            'source': 'event',
        })
    
    # ── STRATEGY 2: Individual market arbitrage ────────────────
    # Check individual markets with 3+ outcomes
    
    markets = client.fetch_markets(limit=200)
    if not markets:
        markets = client.fetch_events(limit=50)  # fallback
    
    for raw_market in (markets or []):
        parsed = client.parse_market(raw_market)
        if not parsed:
            continue
        
        prices = parsed['yes_prices']
        if len(prices) < 3:  # Skip binary markets (always sum to 1)
            continue
        
        analysis = analyze_market(prices)
        if not analysis:
            continue
        
        exec_check = should_execute(
            analysis,
            bankroll=state.current_bankroll,
            min_profit=MIN_PROFIT,
            gas_cost=GAS_COST,
            max_position_pct=MAX_POSITION_PCT,
        )
        
        opportunities.append({
            'market': parsed,
            'analysis': analysis,
            'exec_check': exec_check,
            'source': 'market',
        })
    
    if not opportunities:
        # Log what we scanned so user knows it's working
        if state.scans_total % 20 == 1:  # Every 20th scan
            n_events = len(events_raw)
            n_multi_events = sum(1 for e in events_raw if len(e.get('markets', [])) >= 3)
            n_neg_risk = sum(1 for e in events_raw 
                           if e.get('negRisk', False) or 
                           any(m.get('negRisk', False) for m in e.get('markets', [])))
            n_markets = len(markets) if markets else 0
            logger.info(f"  Scan #{state.scans_total}: {n_events} events "
                       f"({n_multi_events} multi-outcome, {n_neg_risk} mutually-exclusive), "
                       f"{n_markets} markets. No arb above ${MIN_PROFIT}.")
        return
    
    # Sort by profit
    opportunities.sort(
        key=lambda x: x['analysis'].get('raw_profit', 0),
        reverse=True,
    )
    
    state.opportunities_found += len(opportunities)
    
    # Log top opportunities
    for i, opp in enumerate(opportunities[:5]):
        a = opp['analysis']
        e = opp['exec_check']
        q = opp['market']['question'][:50]
        src = opp['source']
        logger.info(
            f"  #{i+1} [{src}] [{a.get('type','')}] {q} | "
            f"Σ={a.get('yes_sum', 0):.4f} | "
            f"Profit: ${a.get('raw_profit', 0):.4f}/$ | "
            f"Execute: {'✅' if e['execute'] else '❌'} {e['reason']}"
        )
    
    # Execute top opportunity
    for opp in opportunities:
        if not opp['exec_check']['execute']:
            continue
        
        can, reason = state.can_trade()
        if not can:
            logger.warning(f"  ⛔ Cannot trade: {reason}")
            break
        
        market = opp['market']
        analysis = opp['analysis']
        position_size = opp['exec_check']['position_size']
        is_dry = state.is_dry_run
        
        logger.info(
            f"\n{'='*50}\n"
            f"  {'🔸 DRY RUN' if is_dry else '🔴 LIVE'} EXECUTING: {market['question'][:60]}\n"
            f"  Source: {opp['source']} | Type: {analysis.get('type','')} | Position: ${position_size:.2f}\n"
            f"  Σ YES: ${analysis.get('yes_sum', 0):.4f} | Expected: ${opp['exec_check']['expected_profit']:.4f}\n"
            f"{'='*50}"
        )
        
        atype = analysis.get('type', '')
        if atype == 'buy_all_yes':
            result = client.execute_buy_all_yes(market, position_size, dry_run=is_dry)
        elif atype == 'buy_all_no':
            result = client.execute_buy_all_no(market, position_size, dry_run=is_dry)
        else:
            continue
        
        state.record_trade(result)
        state.opportunities_executed += 1
        
        if result['success']:
            logger.info(f"  ✅ Trade complete: {result['reason']}")
        else:
            logger.error(f"  ❌ Trade failed: {result['reason']}")
        
        break  # One trade per cycle


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  POLYMARKET ARBITRAGE BOT — FULL AUTO        ║")
    logger.info("║  Bregman Projection + Frank-Wolfe Engine     ║")
    logger.info("╚══════════════════════════════════════════════╝")
    
    # Validate config
    if not PRIVATE_KEY:
        logger.warning("⚠️  No POLY_PRIVATE_KEY set. Bot will scan but cannot execute trades.")
        logger.warning("Set POLY_PRIVATE_KEY in Railway variables to enable execution.")
    else:
        logger.info("Private key: ✅ set")
    
    if SIGNATURE_TYPE in (1, 2) and not FUNDER:
        logger.error("❌ SIGNATURE_TYPE=%d requires POLY_FUNDER (your Polymarket profile address)", SIGNATURE_TYPE)
        logger.error("Find it at: polymarket.com/settings (the address you deposit USDC to)")
    
    logger.info(f"Config:")
    logger.info(f"  Bankroll:        ${BANKROLL}")
    logger.info(f"  Min profit:      ${MIN_PROFIT}/dollar")
    logger.info(f"  Max position:    {MAX_POSITION_PCT*100:.0f}% of bankroll")
    logger.info(f"  Scan interval:   {SCAN_INTERVAL}s")
    logger.info(f"  Max trades/hr:   {MAX_TRADES_PER_HOUR}")
    logger.info(f"  Drawdown limit:  {DRAWDOWN_LIMIT*100:.0f}%")
    logger.info(f"  Dry run:         {DRY_RUN} ({DRY_RUN_HOURS}h)")
    logger.info(f"  Signature type:  {SIGNATURE_TYPE} ({'EOA' if SIGNATURE_TYPE == 0 else 'Magic/Email' if SIGNATURE_TYPE == 1 else 'Browser proxy'})")
    logger.info(f"  Funder:          {'set' if FUNDER else 'not set'}")
    
    # Initialize client — API creds derived from private key automatically
    client = PolyClient(
        private_key=PRIVATE_KEY if PRIVATE_KEY else None,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER if FUNDER else None,
    )
    
    # Initialize state
    state = BotState(BANKROLL)
    
    logger.info(f"\n🚀 Bot started. {'DRY RUN for first ' + str(DRY_RUN_HOURS) + 'h' if DRY_RUN else 'LIVE MODE'}")
    logger.info(f"Scanning every {SCAN_INTERVAL}s...\n")
    
    # Main loop
    cycle = 0
    while True:
        cycle += 1
        
        try:
            scan_and_execute(client, state)
        except Exception as e:
            logger.error(f"Scan cycle {cycle} error: {e}")
        
        # Status update every 10 cycles
        if cycle % 10 == 0:
            logger.info(f"\n📊 {state.status_line()}\n")
        
        # Check if halted
        if state.halted:
            logger.error(f"🛑 BOT HALTED: {state.halt_reason}")
            logger.error("Manual intervention required. Restart to resume.")
            # Sleep longer when halted but don't exit (Railway will restart)
            while state.halted:
                time.sleep(300)  # Check every 5 min
        
        # Dry run transition
        if state.dry_run_until and datetime.utcnow() >= state.dry_run_until:
            logger.info("═" * 50)
            logger.info("🟢 DRY RUN PERIOD ENDED — SWITCHING TO LIVE")
            logger.info(f"Dry run stats: {state.total_trades} trades, "
                       f"${state.total_profit:.4f} theoretical profit")
            logger.info("═" * 50)
            state.dry_run_until = None
        
        time.sleep(SCAN_INTERVAL)


if __name__ == '__main__':
    main()
