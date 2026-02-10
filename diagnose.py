"""
Diagnostic: What does the scanner actually see?
Run this on Railway or locally to understand why 0 opportunities.
"""

import requests
import json

GAMMA_API = "https://gamma-api.polymarket.com"

def diagnose():
    print("=" * 60)
    print("POLYMARKET SCANNER DIAGNOSTIC")
    print("=" * 60)

    # Fetch markets
    print("\n1. Fetching markets...")
    resp = requests.get(f"{GAMMA_API}/markets",
                        params={'limit': 200, 'active': 'true', 'closed': 'false'},
                        timeout=15)
    markets = resp.json()
    print(f"   Got {len(markets)} markets")

    # Categorize
    binary = []
    multi = []
    neg_risk = []
    no_prices = []

    for m in markets:
        raw_outcomes = m.get('outcomes', '[]')
        raw_prices = m.get('outcomePrices', '[]')

        if isinstance(raw_outcomes, str):
            try: raw_outcomes = json.loads(raw_outcomes)
            except: raw_outcomes = []
        if isinstance(raw_prices, str):
            try: raw_prices = json.loads(raw_prices)
            except: raw_prices = []

        prices = [float(p) for p in raw_prices] if raw_prices else []

        if not prices:
            no_prices.append(m.get('question', '?'))
            continue

        if m.get('negRisk'):
            neg_risk.append(m)

        if len(prices) == 2:
            binary.append({
                'q': m.get('question', '?')[:60],
                'prices': prices,
                'sum': sum(prices),
            })
        else:
            multi.append({
                'q': m.get('question', '?')[:60],
                'prices': prices,
                'sum': sum(prices),
                'n': len(prices),
            })

    print(f"\n2. Market breakdown:")
    print(f"   Binary (2 outcomes):  {len(binary)}")
    print(f"   Multi (3+ outcomes):  {len(multi)}")
    print(f"   NegRisk markets:      {len(neg_risk)}")
    print(f"   No prices:            {len(no_prices)}")

    # Binary analysis
    print(f"\n3. Binary markets — price sums:")
    deviations = [(b['sum'], b['q']) for b in binary]
    deviations.sort(key=lambda x: abs(x[0] - 1.0), reverse=True)
    for s, q in deviations[:10]:
        dev = abs(s - 1.0)
        flag = " *** ARB" if dev > 0.02 else ""
        print(f"   Σ={s:.4f} (dev={dev:.4f}){flag}  {q}")

    # Multi analysis
    print(f"\n4. Multi-outcome markets — price sums:")
    if multi:
        for m in sorted(multi, key=lambda x: abs(x['sum'] - 1.0), reverse=True)[:10]:
            dev = abs(m['sum'] - 1.0)
            flag = " *** ARB" if dev > 0.02 else ""
            print(f"   Σ={m['sum']:.4f} (dev={dev:.4f}) [{m['n']} outcomes]{flag}  {m['q']}")
    else:
        print("   NONE FOUND — this is likely why we see 0 opportunities")

    # Check events for grouped markets
    print(f"\n5. Checking EVENTS for multi-condition grouping...")
    resp2 = requests.get(f"{GAMMA_API}/events",
                         params={'limit': 50, 'active': 'true', 'closed': 'false'},
                         timeout=15)
    events = resp2.json()
    print(f"   Got {len(events)} events")

    event_arbs = []
    for ev in events:
        ev_markets = ev.get('markets', [])
        if len(ev_markets) < 3:
            continue

        # For each market in the event, get its YES price
        yes_prices = []
        names = []
        for em in ev_markets:
            raw_p = em.get('outcomePrices', '[]')
            if isinstance(raw_p, str):
                try: raw_p = json.loads(raw_p)
                except: raw_p = []
            if raw_p:
                # First price is typically the YES price
                yes_prices.append(float(raw_p[0]))
                names.append(em.get('question', '?')[:40])

        if len(yes_prices) >= 3:
            s = sum(yes_prices)
            dev = abs(s - 1.0)
            event_arbs.append({
                'title': ev.get('title', '?')[:60],
                'n': len(yes_prices),
                'sum': s,
                'dev': dev,
                'prices': yes_prices[:6],
            })

    print(f"\n6. Event-level arbitrage (THIS is where the real arb lives):")
    event_arbs.sort(key=lambda x: x['dev'], reverse=True)
    for ea in event_arbs[:15]:
        flag = " *** ARB" if ea['dev'] > 0.02 else ""
        prices_str = ", ".join(f"{p:.3f}" for p in ea['prices'])
        print(f"   Σ={ea['sum']:.4f} (dev={ea['dev']:.4f}) [{ea['n']} mkts]{flag}")
        print(f"     {ea['title']}")
        print(f"     Prices: [{prices_str}]")

    # Summary
    print(f"\n{'=' * 60}")
    print("DIAGNOSIS:")
    mkt_arbs = sum(1 for b in binary if abs(b['sum'] - 1.0) > 0.02)
    multi_arbs = sum(1 for m in multi if abs(m['sum'] - 1.0) > 0.02)
    event_arb_count = sum(1 for ea in event_arbs if ea['dev'] > 0.02)

    if mkt_arbs == 0 and multi_arbs == 0 and event_arb_count == 0:
        print("No arbitrage found at >2% threshold anywhere.")
        print("Markets are currently efficient. This is normal.")
        print("\nTry:")
        print("  - Lower MIN_PROFIT to 0.01 (1 cent)")
        print("  - The bot needs to scan EVENT-level sums, not just market-level")
        print("  - Real arb often appears briefly then gets snapped up in <1 min")
    elif event_arb_count > 0:
        print(f"Found {event_arb_count} event-level arbitrage opportunities!")
        print("The bot needs to scan events, not just individual markets.")
    else:
        print(f"Binary arbs: {mkt_arbs}, Multi arbs: {multi_arbs}")

    print("=" * 60)


if __name__ == '__main__':
    diagnose()
