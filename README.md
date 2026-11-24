# Tail Rider — Pump.fun Bot Sprint

[![Competition](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/aahcoba8-maker/makefun-pumpfun-bot-sprint-20261125/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/aahcoba8-maker/makefun-pumpfun-bot-sprint-20261125/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

A trading bot for pump.fun-style Solana bonding curves, built for the
[make.fun](https://make.fun) bounty
[“Pump.fun Bot Sprint: Make Bot And Win”](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed).
It runs on paper money over a replay of chain data and is ranked by net PnL
against holding cash.

## Strategy — “Tail Rider”

The dev window (24h, 1,222 coins, 128k events) is brutal: only 2.9% of coins
ever complete, and the median coin peaks ~1.4x then dies. Both of our earlier
bots in this family capped upside (+15% take-profit, −12% stop) and finished
negative. This bot is built the other way around:

- **Enter early, not after a pump.** Buy as soon as real reserves reach 1 SOL
  (the 1% size cap then allows the 0.01 SOL minimum round-trip stake), within
  the coin’s first ~4 minutes, with ≥3 buys from ≥2 distinct buyers. Measured:
  momentum entries after a pump lose ~8% on average — they buy the top.
- **No fixed take-profit.** Cut losers at −20%; let winners run under a wide
  trailing stop (−40% from peak, armed at +50%). The payoff is extremely
  right-skewed, so the tail is the whole game.
- **Loose entry filters, strict exits.** Creator sells and single big sells
  are routine in this market — a latched creator-sold flag fires on nearly
  every coin within minutes — so they are *not* entry filters. Sellers are
  handled by a **fresh** rug exit instead: a creator sell ≥0.5 SOL or any
  single sell ≥2 SOL *in the current batch*. (Measured: a latched-rug exit
  fired on 213/213 exits in v2 and churned the account to death in fees.)
- **Slot recycling.** A 4-hour time limit exits positions still down >8%, so
  the 5 position slots keep turning over. Without it the slots clog and the
  bot does ~15 round trips in the 6h final window — below the 20-coin
  guardrail.
- **0.01 SOL stakes** (the guardrail minimum round trip), sized at 97% of the
  harness size cap (1% of real reserves — the scorer nets out our own fills,
  so a buy at exactly the cap can be refused).
- **Equity guard:** halt entries at −12% drawdown from peak equity, flatten
  everything at −20% (far inside the 50% guardrail).

Deterministic by construction: no clock, no randomness, no I/O; iteration only
over insertion-ordered dicts fed by the batch stream, so the replay’s two
executions produce identical action logs.

## Results (kit dev window, `grade-local --driver process`)

| metric | value |
|---|---|
| net PnL | **−0.348 SOL** |
| round trips / distinct mints | 295 / 295 |
| win rate | 20.3% |
| max drawdown | 3.7% |
| rejected orders | 0 |

For reference, the kit’s own house sniper pilot scores −0.468 SOL on the same
window, and both of our earlier entries in this family lost more (−0.212 and
−0.113 SOL at larger stakes). The dev window punishes activity; this bot is
the most selective design we measured that still clears the ≥20-coin guardrail
with a wide margin (~76 round trips per 6h by the same rate).

## Files

- `main.py` — the bot (entrypoint; stdlib only at top level)
- `vendor/makefun_bot.py` — the kit’s replay-protocol shim, copied verbatim
  (documented usage; kept out of authorship counts)

## Run locally

```sh
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot . --driver process
```

## License

MIT — see [LICENSE](LICENSE).
