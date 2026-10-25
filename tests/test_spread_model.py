import math

import pytest

from friction_radar.spread_model import (
    Direction, GasModel, PerpVenue, V3Pool, best_direction, breakeven_gross_bps,
    evaluate, optimal_size,
)

P0 = 3000.0
L = 5e6  # deep pool


def pool(fee=0.0005, liq=L, **kw):
    return V3Pool(sqrt_price=math.sqrt(P0), liquidity=liq, fee=fee, **kw)


def test_pool_validation():
    with pytest.raises(ValueError):
        V3Pool(sqrt_price=0, liquidity=1)
    with pytest.raises(ValueError):
        V3Pool(sqrt_price=10, liquidity=1, fee=1.2)
    with pytest.raises(ValueError):
        V3Pool(sqrt_price=10, liquidity=1, sqrt_price_lower=11)


def test_small_swap_converges_to_spot_minus_fee():
    p = pool()
    out = p.sell_base(1e-4)
    assert out / 1e-4 == pytest.approx(P0 * (1 - p.fee), rel=1e-6)
    got = p.buy_base(1e-2)
    assert 1e-2 / got == pytest.approx(P0 / (1 - p.fee), rel=1e-5)


def test_price_impact_is_convex():
    p = pool(fee=0.0)
    avg = [p.sell_base(x) / x for x in (1, 10, 100)]
    assert avg[0] > avg[1] > avg[2]
    # second-order: impact grows roughly linearly in size
    i1, i2 = P0 - avg[1], P0 - avg[2]
    assert i2 / i1 == pytest.approx(10, rel=0.05)


def test_exact_output_inverts_exact_input():
    p = pool()
    dy = p.quote_to_buy_base(5.0)
    assert p.buy_base(dy) == pytest.approx(5.0, rel=1e-9)


def test_range_bounds_enforced():
    s = math.sqrt(P0)
    p = pool(liq=1e4, sqrt_price_lower=s * 0.99, sqrt_price_upper=s * 1.01)
    with pytest.raises(ValueError, match="lower tick"):
        p.sell_base(100)
    with pytest.raises(ValueError, match="upper tick"):
        p.quote_to_buy_base(5)
    with pytest.raises(ValueError):
        pool(liq=10).quote_to_buy_base(1e6)


def test_zero_or_negative_swaps():
    p = pool()
    assert p.sell_base(0) == 0.0 and p.buy_base(-1) == 0.0


def test_gas_cost():
    assert GasModel(100_000, 10, 2000).cost_quote == pytest.approx(2.0)


def test_perp_fill_price_crosses_spread():
    v = PerpVenue(mark_price=P0, half_spread_bps=2)
    assert v.fill_price(True) == pytest.approx(P0 * 1.0002)
    assert v.fill_price(False) == pytest.approx(P0 * 0.9998)


def test_no_basis_is_unprofitable():
    q = evaluate(pool(), PerpVenue(P0), GasModel(), 1.0, Direction.BUY_DEX_SHORT_PERP)
    assert not q.profitable
    assert q.net_pnl < -q.gas_cost  # fees stack on top of gas


def test_wide_basis_buy_dex_profitable():
    perp = PerpVenue(P0 * 1.01)
    assert best_direction(pool(), perp) is Direction.BUY_DEX_SHORT_PERP
    q = evaluate(pool(), perp, GasModel(), 10.0, Direction.BUY_DEX_SHORT_PERP)
    assert q.profitable and q.gross_spread_bps == pytest.approx(100)
    assert 0 < q.net_bps < 100


def test_inverted_basis_sell_dex_profitable():
    perp = PerpVenue(P0 * 0.99)
    d = best_direction(pool(), perp)
    assert d is Direction.SELL_DEX_LONG_PERP
    q = evaluate(pool(), perp, GasModel(), 10.0, d)
    assert q.profitable and q.gross_spread_bps == pytest.approx(100, rel=1e-6)


def test_funding_sign_by_direction():
    perp = PerpVenue(P0 * 1.01, funding_rate_8h=0.0001)
    short = evaluate(pool(), perp, GasModel(), 1.0, Direction.BUY_DEX_SHORT_PERP, hold_hours=8)
    long_ = evaluate(pool(), perp, GasModel(), 1.0, Direction.SELL_DEX_LONG_PERP, hold_hours=8)
    assert short.funding_pnl > 0 > long_.funding_pnl


def test_gas_dominates_tiny_size():
    perp = PerpVenue(P0 * 1.003)
    gas = GasModel(gas_price_gwei=100)
    tiny = evaluate(pool(), perp, gas, 0.01, Direction.BUY_DEX_SHORT_PERP)
    big = evaluate(pool(), perp, gas, 20.0, Direction.BUY_DEX_SHORT_PERP)
    assert not tiny.profitable and big.profitable


def test_optimal_size_is_interior_maximum():
    thin = pool(liq=2e5)
    perp = PerpVenue(P0 * 1.01)
    gas = GasModel()
    best = optimal_size(thin, perp, gas, max_size=200.0)
    assert 0 < best.size_base < 200
    for s in (best.size_base * 0.8, best.size_base * 1.2):
        q = evaluate(thin, perp, gas, s, best.direction)
        assert q.net_pnl <= best.net_pnl + 1e-6


def test_breakeven_matches_evaluate():
    p, gas, size = pool(), GasModel(), 5.0
    be = breakeven_gross_bps(p, PerpVenue(P0), gas, size)
    above = evaluate(p, PerpVenue(P0 * (1 + (be + 2) / 1e4)), gas, size,
                     Direction.BUY_DEX_SHORT_PERP)
    below = evaluate(p, PerpVenue(P0 * (1 + (be - 2) / 1e4)), gas, size,
                     Direction.BUY_DEX_SHORT_PERP)
    assert above.profitable and not below.profitable


def test_evaluate_rejects_bad_size():
    with pytest.raises(ValueError):
        evaluate(pool(), PerpVenue(P0), GasModel(), 0, Direction.BUY_DEX_SHORT_PERP)
