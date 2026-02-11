"""
Arbitrage Math Engine (Production)
===================================
Lightweight version of the Bregman/Frank-Wolfe engine for 24/7 scanning.
Focuses on the most profitable pattern: single-market Σ YES ≠ $1.
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Dict
import time


@dataclass
class Opportunity:
    """Detected arbitrage opportunity ready for execution."""
    market_id: str
    question: str
    slug: str
    opp_type: str              # 'buy_all_yes', 'sell_all_yes', 'buy_all_no'
    yes_sum: float             # Sum of all YES prices
    deviation: float           # |yes_sum - 1.0|
    raw_profit_per_dollar: float
    prices: List[float]        # YES prices per outcome
    outcomes: List[str]
    token_ids: List[str]       # Token IDs for execution
    bregman_D: float           # Bregman divergence
    fw_gap: float              # Frank-Wolfe gap
    guaranteed_profit: float   # D - gap (Proposition 4.1)
    alpha_captured: float      # % of max arbitrage captured
    kelly_size: float          # Recommended position in dollars
    expected_pnl: float        # Expected profit after sizing
    timestamp: float


def analyze_market(prices: List[float]) -> Optional[Dict]:
    """
    Fast single-market arbitrage check.
    
    Returns analysis dict or None if no opportunity.
    """
    n = len(prices)
    if n < 2:
        return None
    
    yes_sum = sum(prices)
    deviation = abs(yes_sum - 1.0)
    
    # Quick reject — but be generous, real arb can be small
    if deviation < 0.01:
        return None
    
    # Determine type
    if yes_sum < 0.99:
        opp_type = 'buy_all_yes'
        raw_profit = 1.0 - yes_sum
    elif yes_sum > 1.01:
        opp_type = 'buy_all_no'
        raw_profit = yes_sum - 1.0
    else:
        return None
    
    # Bregman projection (project onto probability simplex)
    safe = [max(p, 0.005) for p in prices]
    s = sum(safe)
    proj = [p / s for p in safe]
    
    # KL divergence
    p_norm = [p / s for p in safe]
    D = sum(proj[i] * np.log(proj[i] / max(p_norm[i], 1e-15)) for i in range(n))
    D = abs(D)
    
    # FW gap estimate
    grad = [np.log(max(p, 1e-15)) + 1 for p in proj]
    min_g = min(grad)
    vertex = [1.0 if g == min_g else 0.0 for g in grad]
    gap = abs(sum(grad[i] * (proj[i] - vertex[i]) for i in range(n)))
    
    guaranteed = max(0, D - gap)
    alpha = min((D - gap) / D, 1.0) if D > 1e-6 else 0
    
    return {
        'n': n,
        'yes_sum': yes_sum,
        'deviation': deviation,
        'type': opp_type,
        'raw_profit': raw_profit,
        'bregman_D': D,
        'fw_gap': gap,
        'guaranteed_profit': guaranteed,
        'alpha_captured': alpha,
    }


def kelly_position(profit_pct: float, bankroll: float, 
                    exec_prob: float = 0.95, max_frac: float = 0.10) -> float:
    """
    Modified Kelly criterion for prediction market arbitrage.
    
    Arbitrage is special: if all legs fill, profit is GUARANTEED.
    The only risk is partial fills (some legs fail).
    
    For FOK orders on Polymarket, exec_prob ≈ 0.95 (fill or reject instantly).
    
    Simplified: f = (exec_prob * profit - (1 - exec_prob) * loss_on_fail) / profit
    Assuming loss on failed execution ≈ 5% of position (partial fill slippage):
    """
    if profit_pct <= 0 or exec_prob <= 0:
        return 0.0
    
    # Expected value per dollar: prob_success * profit - prob_fail * estimated_loss
    loss_on_fail = 0.05  # Estimate: lose 5% on partial fill / failed execution
    ev_per_dollar = exec_prob * profit_pct - (1 - exec_prob) * loss_on_fail
    
    if ev_per_dollar <= 0:
        return 0.0
    
    # Kelly fraction: EV / profit (simplified for binary outcome)
    f = ev_per_dollar / profit_pct
    
    # Half-Kelly for safety (standard practice)
    f = f * 0.5
    
    f = max(f, 0.0)
    
    position = f * bankroll
    position = min(position, max_frac * bankroll)
    
    return position


def should_execute(opp: Dict, bankroll: float, min_profit: float = 0.03,
                   gas_cost: float = 0.005, max_position_pct: float = 0.10) -> Dict:
    """
    Full pre-execution check.
    
    Returns: {
        'execute': bool,
        'reason': str,
        'position_size': float,
        'expected_profit': float,
    }
    """
    profit = opp['raw_profit']
    
    # Check 1: Minimum profit threshold
    if profit < min_profit:
        return {'execute': False, 'reason': f'Profit ${profit:.4f} < min ${min_profit}',
                'position_size': 0, 'expected_profit': 0}
    
    # Check 2: Position sizing
    position = kelly_position(profit, bankroll, max_frac=max_position_pct)
    if position < 1.0:  # Less than $1 not worth it
        return {'execute': False, 'reason': f'Kelly size ${position:.2f} too small',
                'position_size': 0, 'expected_profit': 0}
    
    # Check 3: Gas cost check
    expected_profit = position * profit
    if gas_cost / expected_profit > 0.30:  # Gas > 30% of profit
        return {'execute': False, 'reason': f'Gas {gas_cost/expected_profit*100:.0f}% of profit',
                'position_size': 0, 'expected_profit': 0}
    
    net_profit = expected_profit - gas_cost
    
    return {
        'execute': True,
        'reason': f'Profit ${net_profit:.4f} after gas',
        'position_size': position,
        'expected_profit': net_profit,
    }
