"""Net arbitrage spread model: Uniswap v3 concentrated liquidity vs CEX perpetuals.

Friction stack (all expressed in quote currency, e.g. USDC):
    DEX: pool fee tier + price impact inside the active tick range + gas
    CEX: taker fee + half bid/ask spread + funding accrued over holding horizon

Swap math follows the Uniswap v3 whitepaper for a single active range with
virtual liquidity L and sqrt price sqrtP = sqrt(token1 / token0):
    token0 in : sqrtP' = L * sqrtP / (L + dx * sqrtP),  dy_out = L * (sqrtP - sqrtP')
    token1 in : sqrtP' = sqrtP + dy / L,                dx_out = L * (1/sqrtP - 1/sqrtP')
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    BUY_DEX_SHORT_PERP = "buy_dex_short_perp"  # DEX cheaper than perp
    SELL_DEX_LONG_PERP = "sell_dex_long_perp"  # DEX richer than perp


@dataclass(frozen=True)
class V3Pool:
    sqrt_price: float           # sqrt(quote per base)
    liquidity: float            # active virtual liquidity L
    fee: float = 0.0005         # 5 bps tier
    sqrt_price_lower: float = 0.0
    sqrt_price_upper: float = math.inf

    def __post_init__(self) -> None:
        if self.sqrt_price <= 0 or self.liquidity <= 0:
            raise ValueError("sqrt_price and liquidity must be positive")
        if not 0 <= self.fee < 1:
            raise ValueError("fee must be in [0, 1)")
        if not self.sqrt_price_lower <= self.sqrt_price <= self.sqrt_price_upper:
            raise ValueError("price outside active range")

    @property
    def price(self) -> float:
        return self.sqrt_price ** 2

    def sell_base(self, dx: float) -> float:
        """Swap dx base (token0) in, return quote (token1) out."""
        if dx <= 0:
            return 0.0
        dx_net = dx * (1 - self.fee)
        L, s = self.liquidity, self.sqrt_price
        s_new = L * s / (L + dx_net * s)
        if s_new < self.sqrt_price_lower:
            raise ValueError("swap crosses lower tick: insufficient range liquidity")
        return L * (s - s_new)

    def buy_base(self, dy: float) -> float:
        """Swap dy quote (token1) in, return base (token0) out."""
        if dy <= 0:
            return 0.0
        dy_net = dy * (1 - self.fee)
        L, s = self.liquidity, self.sqrt_price
        s_new = s + dy_net / L
        if s_new > self.sqrt_price_upper:
            raise ValueError("swap crosses upper tick: insufficient range liquidity")
        return L * (1 / s - 1 / s_new)

    def quote_to_buy_base(self, dx_out: float) -> float:
        """Exact-output: quote required (incl. fee) to receive dx_out base."""
        L, s = self.liquidity, self.sqrt_price
        inv_new = 1 / s - dx_out / L
        if inv_new <= 0:
            raise ValueError("output exceeds pool depth")
        s_new = 1 / inv_new
        if s_new > self.sqrt_price_upper:
            raise ValueError("swap crosses upper tick: insufficient range liquidity")
        return L * (s_new - s) / (1 - self.fee)


@dataclass(frozen=True)
class PerpVenue:
    mark_price: float
    taker_fee: float = 0.0005
    half_spread_bps: float = 1.0
    funding_rate_8h: float = 0.0001  # positive: longs pay shorts

    def fill_price(self, is_buy: bool) -> float:
        adj = self.half_spread_bps / 1e4
        return self.mark_price * (1 + adj if is_buy else 1 - adj)


@dataclass(frozen=True)
class GasModel:
    gas_units: int = 180_000
    gas_price_gwei: float = 20.0
    native_price_quote: float = 3000.0

    @property
    def cost_quote(self) -> float:
        return self.gas_units * self.gas_price_gwei * 1e-9 * self.native_price_quote


@dataclass(frozen=True)
class SpreadQuote:
    direction: Direction
    size_base: float
    gross_spread_bps: float
    dex_quote_flow: float
    perp_quote_flow: float
    gas_cost: float
    perp_fee: float
    funding_pnl: float
    net_pnl: float

    @property
    def net_bps(self) -> float:
        notional = abs(self.perp_quote_flow) or 1.0
        return self.net_pnl / notional * 1e4

    @property
    def profitable(self) -> bool:
        return self.net_pnl > 0


def evaluate(pool: V3Pool, perp: PerpVenue, gas: GasModel, size_base: float,
             direction: Direction, hold_hours: float = 0.0) -> SpreadQuote:
    """Delta-neutral round trip PnL for size_base, net of all frictions."""
    if size_base <= 0:
        raise ValueError("size_base must be positive")
    gross = (perp.mark_price - pool.price) / pool.price * 1e4
    periods = hold_hours / 8.0
    if direction is Direction.BUY_DEX_SHORT_PERP:
        dex_flow = -pool.quote_to_buy_base(size_base)
        px = perp.fill_price(is_buy=False)
        perp_flow = size_base * px
        funding = perp.funding_rate_8h * periods * perp_flow  # short receives
    else:
        dex_flow = pool.sell_base(size_base)
        px = perp.fill_price(is_buy=True)
        perp_flow = -size_base * px
        funding = -perp.funding_rate_8h * periods * abs(perp_flow)  # long pays
        gross = -gross
    fee = abs(perp_flow) * perp.taker_fee
    net = dex_flow + perp_flow - fee - gas.cost_quote + funding
    return SpreadQuote(direction, size_base, gross, dex_flow, perp_flow,
                       gas.cost_quote, fee, funding, net)


def best_direction(pool: V3Pool, perp: PerpVenue) -> Direction:
    return (Direction.BUY_DEX_SHORT_PERP if perp.mark_price >= pool.price
            else Direction.SELL_DEX_LONG_PERP)


def optimal_size(pool: V3Pool, perp: PerpVenue, gas: GasModel, max_size: float,
                 hold_hours: float = 0.0, tol: float = 1e-6) -> SpreadQuote:
    """Golden-section search on net PnL (concave in size: linear edge - convex impact)."""
    d = best_direction(pool, perp)
    f = lambda x: evaluate(pool, perp, gas, x, d, hold_hours).net_pnl  # noqa: E731
    g = (math.sqrt(5) - 1) / 2
    lo, hi = tol, max_size
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    fa, fb = f(a), f(b)
    while hi - lo > tol * max(1.0, max_size):
        if fa < fb:
            lo, a, fa = a, b, fb
            b = lo + g * (hi - lo)
            fb = f(b)
        else:
            hi, b, fb = b, a, fa
            a = hi - g * (hi - lo)
            fa = f(a)
    return evaluate(pool, perp, gas, (lo + hi) / 2, d, hold_hours)


def breakeven_gross_bps(pool: V3Pool, perp: PerpVenue, gas: GasModel, size_base: float) -> float:
    """Minimum gross basis (bps) needed to cover frictions at a given size (linear approx)."""
    notional = size_base * pool.price
    impact = (pool.quote_to_buy_base(size_base) - notional) / notional * 1e4
    return impact + perp.taker_fee * 1e4 + perp.half_spread_bps + gas.cost_quote / notional * 1e4
