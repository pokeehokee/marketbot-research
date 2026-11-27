# Whale and meme-bot mutations: pre-registered plan (written 2026-11-27, before any data below exists)

The base whale copy test (whales-1) is dead: holdout win 51%, multiple 0.92, -0.015c per $1.
I then looked at its holdout (from 2026-11-03) while exploring, so **that holdout is spent**.
Nothing below may be decided on whales-1's holdout; it is exploratory only.

## Rules for every variant
- Paper only. Venues legal for the operator: Kalshi, Polymarket US, US futures.
  Hyperliquid is a data source only.
- Every variant reports four numbers separately: win rate, payout multiple, profit per $1,
  and sample size (positions, days, losses).
- CIs are clustered by day. A variant survives only if its holdout CI excludes zero after
  Benjamini-Hochberg at q = 0.10 across EVERY variant in this file (count kept below), and
  both halves of the holdout are positive.
- A variant is chosen on train only. The holdout is read once, for the chosen cell.

## Clean holdouts
- **whales-2** (Hyperliquid wallets with $25k-$250k accounts, 900 of them, 150 days, events
  from $10k, with closedPnl): train = events before 2026-09-01; **holdout = 2026-09-01 to
  2026-09-27**. That window predates whales-1, so it has never been looked at.
  The 10,000-fill cap means active wallets may not reach back that far; the coverage file
  says how many did.
- **Kalshi follow (kf-1)**: built on whales-1's events, so its pre-registered primary cell
  (entries, delay 0, all coins, taker, hold to settlement) is graded on 2026-11-03 onward
  as before. It is admissible only because the Kalshi binary outcome was never computed
  before now, but the copy list and event families were shaped on that window, so a pass
  there is "promising", not "proven"; it must re-pass on whales-2's holdout or forward data.
- **Meme bot**: forward data only (meme-rec recorder, started 2026-11-27). Train = the first
  60% of recorded days; holdout = the rest. No variant below is scored before 7 days exist.

## Whale variants (count: 6)
1. Follow on Kalshi 15-minute binaries (kf-1, running).
2. Same, maker: rest a bid at the taker price minus 1c; filled only if a later print trades
   through it within 120 s; missed fills counted as no trade.
3. Small, consistent wallets: on train, wallets with >= 20 closed positions, realised win
   rate >= 60% and positive closedPnl in both halves of train; follow them in holdout.
4. Fade the worst: train's bottom-decile wallets by closedPnl, inverted. (Exploratory read on
   whales-1 was dead; this is its one clean chance.)
5. Front-run the rest of a whale's order: enter at the event's FIRST fill + latency (5 s for
   a cloud box, 30 s for a phone) and exit at its last fill + 60 s. Second-level prices are
   not in hand, so this uses 1-minute opens and says so; it can only show an upper bound.
6. Polymarket sharp wallets: wallets profitable on resolved markets in train; does their
   buying in holdout predict resolution? Mapped to Kalshi or Polymarket US only if so.

Funding-rate extremes were read on whales-1 data (exploratory): of 12 coin/horizon cells,
the sign flips between train and holdout on BTC and only one holdout cell clears zero.
Dead; not counted above.

## Meme-bot base test and variants (count: 7)
Base: the prompt's rules as written, on recorded rows, with realistic fills: entry at +2.5%
slippage plus a Jito-style priority fee, exit slippage plus the AMM fee, a stop that gaps
fills at the candle low, a sell that fails if liquidity is pulled, rug = price -90% or
reserve pulled. Rules that need data not freely available (40 smart-money wallets, KOLs,
dev wallet %, bundled launch, honeypot simulation) are recorded as UNTESTED, not assumed.
Variants:
1. After the rug window: pools aged 24-48h that kept their reserve.
2. Graduation timing: pump.fun pools in the first hour after migration to PumpSwap/Raydium.
3. First retrace: skip the first pump; buy the first -30% pullback that holds reserve.
4. No compounding, small fixed size ($5 a trade) instead of 20-40% of equity.
5. Filter audit: which single filter cuts the rug rate most (liquidity, locked %, top-10
   share, mint/freeze authority, holders), measured as rug-rate ratio with a CI.
6. Long-track-record wallets instead of hot ones: needs wallet-level trade history;
   GeckoTerminal's per-pool trades carry the trader's address, so this is feasible later.
7. The reverse: does the prompt's exact trigger predict a LOSS strongly enough to fade? On
   Solana there is no short; recorded only as a diagnostic, not a strategy.

## Forward test of variants 1 and 2, pre-registered 2026-11-27 (Devin's yes)

Both died on the historical holdout (train negative, holdout positive in every family, entries
about 4x denser after Nov 3), so they are tracked forward, paper only, on data nobody has seen.

- Universe: Hyperliquid leaderboard's 600 largest accounts >= $250k (whales-1's rule), frozen
  in the database on the first run, 2026-11-27. Events >= $50k notional, as whales-1.
- Counted: entries (open/add/flip), delay 0 (+5 s latency), all three coins, hold to settlement,
  from 2026-11-28 UTC on. Two cells only: taker at the ask (Kalshi taker fee), and maker
  (bid = taker price - 1c, filled only on a trade-through within 120 s, maker fee 0.0175).
- Verdict once a cell has >= 20 days AND >= 300 entries: PASS only if its day-clustered
  bootstrap interval at 97.5% (Bonferroni over the two cells) excludes zero AND both halves of
  its days are positive. Anything else is dead. No cell, filter or coin split is added later.
- A PASS is a reason to build a paper book in the app, not to trade.
- Workflow: .github/workflows/whales-forward.yml, daily 07:17Z; score in out/kf-forward.txt.
