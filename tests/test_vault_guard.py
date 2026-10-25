"""Tests for contracts/AtomicFrictionVault.sol.

Two layers:
1. A bit-exact Python reference model of the vault's guard / flash accounting
   (integer math, same revert conditions) exercised against adversarial cases
   and wired to friction_radar.spread_model for minNetProfit sizing.
2. Static invariants on the Solidity source, plus a real solc compile when
   py-solc-x with an installed compiler is available (skipped otherwise).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from friction_radar.spread_model import GasModel, PerpVenue, V3Pool, optimal_size

SRC = (Path(__file__).resolve().parents[1] / "contracts" / "AtomicFrictionVault.sol").read_text()
MAX_BPS = 10_000


class Revert(Exception):
    pass


@dataclass
class VaultModel:
    """Mirror of AtomicFrictionVault state machine (quote balances in wei-like ints)."""
    balance: int
    min_profit_bps: int = 5
    max_notional: int = 10**24
    allowed: set = field(default_factory=lambda: {"router"})
    paused: bool = False
    now: int = 1_000
    _lock: int = 1
    _active_lender: str | None = None

    def required(self, notional: int, min_net: int) -> int:
        if notional > self.max_notional:
            raise Revert("NotionalCap")
        floor_ = notional * self.min_profit_bps // MAX_BPS
        return max(min_net, floor_)

    def _pre(self, deadline: int) -> None:
        if self.paused:
            raise Revert("IsPaused")
        if self.now > deadline:
            raise Revert("Expired")
        if self._lock != 1:
            raise Revert("Reentrancy")

    def _guard(self, pre: int, req: int) -> int:
        delta = self.balance - pre
        if delta < 0 or delta < req:
            raise Revert("NetProfitBelowMin")
        return delta

    def _legs(self, legs) -> None:
        for target, pnl in legs:  # pnl: signed quote delta produced by the leg
            if target not in self.allowed:
                raise Revert("TargetNotAllowed")
            self.balance += pnl

    def execute(self, legs, notional: int, min_net: int, deadline: int) -> int:
        self._pre(deadline)
        req = self.required(notional, min_net)
        self._lock, snap, pre = 2, self.balance, self.balance
        try:
            self._legs(legs)
            return self._guard(pre, req)
        except Revert:
            self.balance = snap  # EVM revert rolls back state
            raise
        finally:
            self._lock = 1

    def flash(self, lender: str, legs, notional: int, fee: int, min_net: int, deadline: int,
              initiator: str = "self") -> int:
        self._pre(deadline)
        req = self.required(notional, min_net)
        self._lock, snap, pre = 2, self.balance, self.balance
        self._active_lender = lender
        try:
            self.balance += notional                      # lender transfers principal
            self.on_flash_loan(lender, initiator, legs)
            if self.balance < notional + fee:
                raise Revert("ERC20InsufficientBalance")  # lender pull fails
            self.balance -= notional + fee                 # lender pulls repayment
            return self._guard(pre, req)
        except Revert:
            self.balance = snap
            raise
        finally:
            self._active_lender, self._lock = None, 1

    def on_flash_loan(self, caller: str, initiator: str, legs) -> None:
        if self._active_lender is None or caller != self._active_lender:
            raise Revert("UntrustedLender")
        if initiator != "self":
            raise Revert("UntrustedInitiator")
        self._legs(legs)


E18 = 10**18


def test_required_takes_max_of_keeper_and_onchain_floor():
    v = VaultModel(balance=0, min_profit_bps=5)
    assert v.required(1_000 * E18, 0) == E18 // 2           # 5 bps of 1000
    assert v.required(1_000 * E18, 3 * E18) == 3 * E18      # keeper stricter wins
    with pytest.raises(Revert, match="NotionalCap"):
        VaultModel(balance=0, max_notional=10).required(11, 0)


@pytest.mark.parametrize("pnl,ok", [(E18, True), (E18 // 2, True), (E18 // 2 - 1, False), (-1, False)])
def test_execute_guard_boundary_and_rollback(pnl, ok):
    v = VaultModel(balance=100 * E18)
    args = ([("router", pnl)], 1_000 * E18, 0, 2_000)
    if ok:
        assert v.execute(*args) == pnl
    else:
        with pytest.raises(Revert, match="NetProfitBelowMin"):
            v.execute(*args)
        assert v.balance == 100 * E18 and v._lock == 1


def test_flash_profit_measured_after_fee():
    v = VaultModel(balance=0, min_profit_bps=0)
    notional, fee = 500 * E18, 500 * E18 * 9 // MAX_BPS      # 9 bps lender fee
    assert v.flash("aave", [("router", fee + E18)], notional, fee, E18, 2_000) == E18
    with pytest.raises(Revert, match="NetProfitBelowMin"):  # gross edge exactly eaten by fee
        v.flash("aave", [("router", fee)], notional, fee, 1, 2_000)
    with pytest.raises(Revert, match="ERC20InsufficientBalance"):
        v.flash("aave", [("router", -2 * E18)], notional, fee, 0, 2_000)  # loss > vault buffer
    assert v.balance == E18 and v._active_lender is None


def test_flash_callback_auth():
    v = VaultModel(balance=0)
    with pytest.raises(Revert, match="UntrustedLender"):   # direct griefing call, no armed lender
        v.on_flash_loan("evil", "self", [])
    with pytest.raises(Revert, match="UntrustedInitiator"):
        v.flash("aave", [], 10, 0, 0, 2_000, initiator="attacker")


def test_liveness_whitelist_and_reentrancy():
    v = VaultModel(balance=E18)
    with pytest.raises(Revert, match="Expired"):
        v.execute([], 0, 0, deadline=999)
    with pytest.raises(Revert, match="TargetNotAllowed"):
        v.execute([("rogue", E18)], 0, 0, 2_000)
    v._lock = 2
    with pytest.raises(Revert, match="Reentrancy"):
        v.execute([], 0, 0, 2_000)
    v._lock, v.paused = 1, True
    with pytest.raises(Revert, match="IsPaused"):
        v.execute([], 0, 0, 2_000)


def test_spread_model_feeds_min_net_profit():
    """Off-chain optimizer -> on-chain floor: haircut expected PnL and verify guard semantics."""
    pool = V3Pool(sqrt_price=math.sqrt(3000.0), liquidity=5e6)
    perp = PerpVenue(mark_price=3012.0)
    q = optimal_size(pool, perp, GasModel(gas_price_gwei=5), max_size=200.0)
    assert q.profitable
    notional = int(q.size_base * pool.price * E18)
    min_net = int(q.net_pnl * 0.5 * E18)                    # 50% haircut for latency/MEV
    v = VaultModel(balance=0, min_profit_bps=1)
    assert v.execute([("router", int(q.net_pnl * E18))], notional, min_net, 2_000) > 0
    with pytest.raises(Revert):                             # adverse fill below haircut
        v.execute([("router", min_net - 1)], notional, min_net, 2_000)


def test_solidity_source_invariants():
    for err in ["NetProfitBelowMin", "UntrustedLender", "UntrustedInitiator", "TargetNotAllowed",
                "NotionalCap", "Reentrancy", "Expired", "IsPaused"]:
        assert re.search(rf"error {err}\(", SRC) and f"revert {err}(" in SRC, err
    assert 'keccak256("ERC3156FlashBorrower.onFlashLoan")' in SRC
    assert "pragma solidity ^0.8" in SRC                    # checked arithmetic
    assert SRC.count("approve(target, 0)") == 1             # allowance reset after each leg
    assert "_activeLender = address(0);" in SRC
    assert "delegatecall" not in SRC and "selfdestruct" not in SRC
    assert re.search(r"function flashRebalance[\s\S]*?onlyKeeper live\(deadline\) nonReentrant", SRC)


def test_solidity_compiles_if_solc_available():
    solcx = pytest.importorskip("solcx")
    if not solcx.get_installed_solc_versions():
        pytest.skip("no local solc installed")
    out = solcx.compile_source(SRC, output_values=["abi"])
    abi = next(v["abi"] for k, v in out.items() if k.endswith(":AtomicFrictionVault"))
    names = {e.get("name") for e in abi}
    assert {"execute", "flashRebalance", "onFlashLoan"} <= names
