"""Tail Rider: a pump.fun bonding-curve replay bot (agent-fun/replay/v2).

Design, from measuring the kit's own dev window (24h, 1222 coins, 128k events):
- Only 2.9% of coins ever complete; the median coin peaks ~1.4x from the moment
  its real SOL reserve first crosses 10 SOL, then dies. The payoff distribution
  is extremely right-skewed: a trailing stop that lets runners run captures the
  tail, while a fixed take-profit caps it. Both of our earlier bots in this
  family capped upside (+15% take-profit) and stopped out at -12%, and both
  finished negative on the dev window. This bot therefore takes NO fixed
  take-profit: it cuts losers at -20% and lets winners run
  under a wide trailing stop (-40% from peak, armed at +50%).
- Entries are EARLY, not after a pump: a coin is bought as soon as its real
  reserves reach 1 SOL (the 1% size cap then allows the 0.01 SOL minimum
  round-trip stake), within its first ~4 minutes, with at least 3 buys from
  2 distinct buyers. Measured: momentum entries after a pump lose ~8% on
  average - they buy the top. The entry tape filters are deliberately loose
  at this size: creator sells and single big sells are routine in this market
  (a latched creator-sold flag fires on nearly every coin within minutes), so
  they are NOT entry filters; the fresh-rug exit handles sellers instead.
- Buys are sized at 97% of the harness size cap (1% of real reserves): the
  scorer's reserve view nets out our own earlier fills, so a buy at exactly the
  cap can land a hair over it and be refused. Stake is 0.01 SOL, the
  guardrail minimum round trip and the smallest stake whose ~4.6% round-trip
  fees do not dominate.
- Exits are mechanical: a hard stop (-20%), a trailing stop once the position
  has run (-40% from peak, armed at +50%), a 4-hour time limit for positions
  still down >8% (this keeps the 5 slots turning over - without it the slots
  clog and the bot does ~15 round trips in the 6h final window, below the
  20-coin guardrail), and an exit on a FRESH rug signal (a creator sell
  >= 0.5 SOL or any single sell >= 2 SOL in the current batch). Latched rug
  flags must NOT be used as an exit: measured 213/213 exits were latched-rug
  exits in v2, churning the account in fees.
- An equity guard halts entries at -12% drawdown from peak equity and flattens
  everything at -20%, far inside the 50% guardrail.

Deterministic by construction: no clock, no randomness, no I/O; iteration only
over insertion-ordered dicts fed by the batch stream, so the replay's two
executions produce identical action logs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

SOL = 1_000_000_000

# --- sizing ---
STAKE_LAMPORTS = 10_000_000          # 0.01 SOL per coin: the guardrail minimum round
                                     # trip, and the smallest stake whose fees
                                     # (~4.6% round trip) do not dominate
MIN_STAKE_LAMPORTS = 10_000_000      # 0.01 SOL: below this a buy does not count
                                     # toward the 20-coin guardrail at all
CAP_HAIRCUT_BPS = 9700               # buy at most 97% of the harness size cap

# --- entry ---
ENTRY_MIN_RQ = 1_000_000_000         # 1 SOL real reserves: enter early, when the
                                     # 1% size cap still allows the 0.01 SOL minimum
                                     # stake; later entries buy the top (measured:
                                     # momentum entries after a pump lose ~8% avg)
ENTRY_MIN_AGE = 0                    # no minimum age: the first minutes are the
                                     # cheapest entry the cap allows
ENTRY_MAX_AGE = 600                  # slots since create (~4 min) after which no entry
TAPE_WINDOW = 300                    # slots of tape the entry signal reads
MIN_NET_FLOW = 0                     # any net SOL inflow counts at this size
MIN_WINDOW_BUYS = 3                  # buy trades in the window, at least
MIN_BUYERS = 2                       # distinct buyers in the window, at least
MIN_UP_BPS = 0                       # no momentum requirement: at 1 SOL of reserves
                                     # the coin has barely moved yet
MAX_UP_BPS = 10000                   # ... but not more than +100% (already vertical)
NEAR_HIGH_BPS = 0                    # no near-high requirement at this stage
DIP_MAX_BPS = 10000                  # optional dip-entry cap: price now <= this % of
                                     # the window high (10000 = disabled)
MAX_SELL_RATIO_BPS = 10000           # no sell-ratio filter: at 1 SOL of reserves the
                                     # tape is too thin for the ratio to mean
                                     # anything; the fresh-rug exit handles sellers
MAX_SINGLE_SELL = 1_000_000_000_000  # (disabled: single-sell entry filter off)
FILTER_DEV_SOLD = False              # dev-sold in the entry window is routine in
                                     # this market, not a signal; the fresh-rug
                                     # exit handles sellers
FILTER_BIG_SELL = False              # (same reason)

# --- exits ---
STOP_BPS = 8000                      # -20% hard stop from entry price (a -12% stop
                                     # was measured to cut the tail before it pays)
TRAIL_ARM_BPS = 15000                # +50% arms the trailing stop
TRAIL_BPS = 6000                     # exit at 60% of the peak price
MAX_HOLD_SLOTS = 36000               # ~4h: a position still down >8% by then is
                                     # recycled so the 5 slots keep turning over
                                     # (the 6h final window needs >= 20 round trips;
                                     # without this the slots clog and it does ~15)
MAX_HOLD_MIN_GAIN_BPS = 9200        # ... unless down less than 8% at the time limit
BIG_SELL_EXIT = 1_000_000_000        # 1 SOL single sell exits a held position
RUG_DEV_SELL = 500_000_000           # creator sell this big (fresh, this batch) = rug
RUG_BIG_SELL = 2_000_000_000         # any single sell this big (fresh, this batch) = rug

# --- equity guard ---
EQ_HALT_BPS = 8800                   # halt entries at -12% equity drawdown
EQ_FLATTEN_BPS = 8000                # flatten everything at -20%


def price_of(curve):
    """SOL per token from a curve's virtual reserves (float, deterministic)."""
    if curve.virtual_token_reserves <= 0:
        return 0.0
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


class Coin:
    """Tape we keep for one watched coin (pruned once it ages out)."""

    __slots__ = ("mint", "created_slot", "creator", "decided", "tape", "prices",
                 "dev_sold", "big_sell_seen")

    def __init__(self, mint, created_slot, creator):
        self.mint = mint
        self.created_slot = created_slot
        self.creator = creator
        self.decided = False
        self.tape = []      # (slot, is_buy, sol_amount, user) within TAPE_WINDOW
        self.prices = []    # (slot, price) within TAPE_WINDOW
        self.dev_sold = False
        self.big_sell_seen = False

    def prune(self, slot):
        cutoff = slot - TAPE_WINDOW
        while self.tape and self.tape[0][0] < cutoff:
            self.tape.pop(0)
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.pop(0)

    def tape_stats(self):
        buys = 0
        buy_sol = 0
        sell_sol = 0
        buyers = set()
        for (_s, is_buy, sol, user) in self.tape:
            if is_buy:
                buys += 1
                buy_sol += sol
                buyers.add(user)
            else:
                sell_sol += sol
        return buys, buy_sol, sell_sol, len(buyers)


class Position:
    """Our own bookkeeping for one held coin (entry anchor + peak)."""

    __slots__ = ("mint", "entry_price", "peak_price", "entry_slot")

    def __init__(self, mint, entry_price, entry_slot):
        self.mint = mint
        self.entry_price = entry_price
        self.peak_price = entry_price
        self.entry_slot = entry_slot


# --- module state (one run only; the grader runs the process fresh) ---
watch = {}        # mint -> Coin, insertion order = first-seen order
positions = {}    # mint -> Position
peak_equity = None
flattened = False


def on_batch(state, batch):
    global peak_equity, flattened
    slot = batch["slot"]
    orders = []
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()

    # --- 1. fold this batch's events into the tape -------------------------
    for e in batch["events"]:
        mint = e["mint"]
        kind = e["event"]
        if kind == "CreateEvent":
            if mint not in watch and mint not in positions:
                watch[mint] = Coin(mint, e["slot"], e.get("creator"))
            coin = watch.get(mint)
            if coin is not None:
                curve = state.curves.get(mint)
                if curve is not None:
                    coin.prices.append((e["slot"], price_of(curve)))
        elif kind == "TradeEvent":
            coin = watch.get(mint)
            if coin is not None:
                is_buy = bool(e["is_buy"])
                sol = int(e["sol_amount"])
                user = e["user"]
                coin.tape.append((e["slot"], is_buy, sol, user))
                curve = state.curves.get(mint)
                if curve is not None:
                    coin.prices.append((e["slot"], price_of(curve)))
                if not is_buy:
                    if user == coin.creator:
                        coin.dev_sold = True
                    if sol >= MAX_SINGLE_SELL:
                        coin.big_sell_seen = True
            # a trade on a held coin updates its peak
            pos = positions.get(mint)
            if pos is not None:
                curve = state.curves.get(mint)
                if curve is not None:
                    p = price_of(curve)
                    if p > pos.peak_price:
                        pos.peak_price = p
        elif kind == "CompleteEvent":
            watch.pop(mint, None)

    # --- 2. prune stale watched coins --------------------------------------
    drop = []
    for mint, coin in watch.items():
        coin.prune(slot)
        if mint in positions:
            continue
        if slot - coin.created_slot > ENTRY_MAX_AGE + TAPE_WINDOW:
            drop.append(mint)
    for mint in drop:
        del watch[mint]

    # --- 3. drop position bookkeeping for coins we no longer hold ----------
    pending_mints = set()
    for p in state.pending.values():
        pending_mints.add(p.order["mint"])
    drop_pos = []
    for mint in positions:
        if mint not in state.positions and mint not in pending_mints:
            drop_pos.append(mint)
    for mint in drop_pos:
        del positions[mint]

    # --- 4. equity guard ----------------------------------------------------
    equity = state.cash
    for mint, holding in state.positions.items():
        curve = state.curves.get(mint)
        if curve is not None:
            equity += int(holding.token_amount * price_of(curve))
    if peak_equity is None or equity > peak_equity:
        peak_equity = equity
    halt_entries = flattened or equity < peak_equity * EQ_HALT_BPS // 10000
    if not flattened and equity < peak_equity * EQ_FLATTEN_BPS // 10000:
        flattened = True
        halt_entries = True

    # --- 5. exits first (frees position slots for fresh entries) -----------
    # Fresh rug signal: a creator sell >= RUG_DEV_SELL or any single sell
    # >= RUG_BIG_SELL in THIS batch. (The latched coin.dev_sold /
    # big_sell_seen flags fire on nearly every coin within minutes of entry
    # in this market, so they must not be used as an exit trigger.)
    rug_now = set()
    for e in batch["events"]:
        if e["event"] == "TradeEvent" and not e["is_buy"]:
            sol = int(e["sol_amount"])
            creator = e.get("creator")
            if (creator is not None and e["user"] == creator and sol >= RUG_DEV_SELL) \
                    or sol >= RUG_BIG_SELL:
                rug_now.add(e["mint"])
    pending_sells = set()
    for p in state.pending.values():
        if p.order["side"] == "sell":
            pending_sells.add(p.order["mint"])
    for mint, holding in list(state.positions.items()):
        if mint in pending_sells:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        pos = positions.get(mint)
        if pos is None:
            # bought before this process saw the decision (should not happen);
            # anchor on the current price so the stops still work.
            pos = Position(mint, price_of(curve), slot)
            positions[mint] = pos
        now = price_of(curve)
        if now > pos.peak_price:
            pos.peak_price = now
        rug = mint in rug_now
        stop_hit = now * 10000 <= pos.entry_price * STOP_BPS
        trail_hit = (pos.peak_price * 10000 >= pos.entry_price * TRAIL_ARM_BPS
                     and now * 10000 <= pos.peak_price * TRAIL_BPS)
        stale = (slot - pos.entry_slot > MAX_HOLD_SLOTS
                 and now * 10000 < pos.entry_price * MAX_HOLD_MIN_GAIN_BPS)
        if rug or stop_hit or trail_hit or stale or flattened:
            if holding.token_amount > 0:
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": int(holding.token_amount),
                               "max_slippage_bps": 10000})

    # --- 6. entries ----------------------------------------------------------
    if not halt_entries:
        free = state.free_cash_lamports()
        slots_free = caps["max_open_positions"] - state.committed_position_count()
        max_orders = caps["max_orders_per_batch"] - len(orders)
        for mint, coin in list(watch.items()):
            if slots_free <= 0 or max_orders <= 0:
                break
            if coin.decided or mint in positions or mint in state.positions:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            age = slot - coin.created_slot
            if age < ENTRY_MIN_AGE or age > ENTRY_MAX_AGE:
                continue
            if curve.real_quote_reserves < ENTRY_MIN_RQ:
                continue
            if FILTER_DEV_SOLD and coin.dev_sold:
                continue
            if FILTER_BIG_SELL and coin.big_sell_seen:
                continue
            buys, buy_sol, sell_sol, n_buyers = coin.tape_stats()
            if buys < MIN_WINDOW_BUYS or n_buyers < MIN_BUYERS:
                continue
            if buy_sol - sell_sol < MIN_NET_FLOW:
                continue
            if sell_sol * 10000 > buy_sol * MAX_SELL_RATIO_BPS:
                continue
            if not coin.prices:
                continue
            start_price = coin.prices[0][1]
            high = max(p for (_s, p) in coin.prices)
            now = price_of(curve)
            if start_price <= 0 or now <= 0:
                continue
            up_bps = int((now - start_price) * 10000 / start_price)
            if up_bps < MIN_UP_BPS or up_bps > MAX_UP_BPS:
                continue
            if now * 10000 < high * NEAR_HIGH_BPS:
                continue
            if now * 10000 > high * DIP_MAX_BPS:
                continue
            cap = state.max_buy_lamports(mint)
            amount = min(STAKE_LAMPORTS, cap * CAP_HAIRCUT_BPS // 10000)
            if amount < MIN_STAKE_LAMPORTS:
                continue
            if free < amount + tx:
                continue
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": int(amount),
                           "max_slippage_bps": 10000})
            coin.decided = True
            positions[mint] = Position(mint, now, slot)
            free -= amount + tx
            slots_free -= 1
            max_orders -= 1

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
