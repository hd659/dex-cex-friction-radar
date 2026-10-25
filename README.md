# dex-cex-friction-radar

A transaction-cost arbitrage radar for on-chain DEX (Uniswap v3) pools against CEX perpetuals, plus a Solidity vault that settles the DEX leg atomically.

The premise comes from Transaction Cost Economics: a basis is only real once every friction is paid. That means pool fee, price impact, gas, taker fee, half-spread, funding and flash-loan fee. The radar works out the net edge off-chain. The vault enforces that edge on-chain, so a trade that has gone stale reverts and does not lose money.

## Layout

```
friction_radar/spread_model.py     # M1: v3 swap math + perp friction stack + optimal sizing
contracts/AtomicFrictionVault.sol  # M2: atomic settlement vault with min net-profit guard
tests/test_spread_model.py         # M1 tests
tests/test_vault_guard.py          # M2 tests: reference model + source invariants + optional solc compile
```

## M1: Friction model

`evaluate()` returns the delta-neutral round-trip PnL in quote currency:

```
net = dex_flow + perp_flow - taker_fee - gas + funding
```

Net PnL is concave in size: the edge grows linearly while impact is convex. `optimal_size()` therefore finds the maximum with a golden-section search. `breakeven_gross_bps()` gives the smallest basis that covers all frictions at a given size.

## M2: AtomicFrictionVault

| Path | Flow |
|------|------|
| `execute(legs, notional, minNetProfit, deadline)` | Uses the vault's own capital. Runs whitelisted router calls, then checks the balance delta. |
| `flashRebalance(lender, legs, notional, minNetProfit, deadline)` | Borrows `notional` via ERC-3156 and runs the legs inside `onFlashLoan`. The lender pulls `principal + fee`, and the guard runs on what is left. |

Guard rule (the same on both paths):

```
required = max(minNetProfit, notional * minProfitBps / 10_000)
revert NetProfitBelowMin unless  balance_after - balance_before >= required
```

- `minNetProfit` comes from the keeper. The usual value is `optimal_size(...).net_pnl * haircut`, which leaves room for latency and MEV.
- `minProfitBps` is a floor set by the owner. A compromised or buggy keeper cannot lower it.
- Profit on the flash path is measured after the lender has been repaid, so the flash fee counts as a friction.

Safety controls:

- Target whitelist. The vault itself and the quote token cannot be whitelisted, which blocks arbitrary `transfer` calldata.
- Each leg gets an exact approval, and the allowance is reset to 0 after the call.
- `nonReentrant` on every entry point. `deadline` and `paused` act as liveness gates. `maxNotional` caps exposure per transaction.
- `onFlashLoan` only accepts calls from the lender armed in that transaction, and only with `initiator == address(this)`. This blocks griefing through externally triggered callbacks.
- No `delegatecall` and no `selfdestruct`. Arithmetic is checked (0.8.x).

The CEX perp hedge stays off-chain. The vault can only guarantee the DEX leg. Hedge risk (fill latency, funding drift) belongs in the haircut on `minNetProfit`.

## Tests

```bash
python -m pytest -q
```

`test_vault_guard.py` has three parts:

1. `VaultModel`, an integer-exact Python mirror of the contract's state machine. It covers the guard boundary at `required - 1`, rollback on revert, flash-fee accounting, failed repayment, callback auth, whitelist, deadline, pause and reentrancy.
2. Static invariants on the `.sol` source: each custom error is declared and used, the ERC-3156 magic value is present, the allowance is reset, the lender is disarmed, and no dangerous opcodes appear.
3. A real `solc` compile with an ABI check. It runs when `py-solc-x` and a compiler are installed (`pip install py-solc-x && python -c "import solcx; solcx.install_solc('0.8.24')"`) and is skipped otherwise.

## Roadmap

- M3: AsyncIO radar loop that streams pool `slot0`/liquidity and perp book/funding, then emits executable quotes.
- M4: Foundry fork tests against live Uniswap v3 routers and Aave/Balancer flash lenders.
