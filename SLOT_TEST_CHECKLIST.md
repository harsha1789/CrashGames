# GameGuard — Slot QA Checklist

The master QA list covers all casino verticals. This is the **slot-only** scope: which master
checks apply to slots, which are out of scope, plus the extra checks GameGuard already automates.
Disconnection/abandon is **deferred** (network-kill is hard to drive reliably right now).

## In scope — slot checks

| # | Check | What it verifies | GameGuard status |
|---|-------|------------------|--------------|
| 1 | **Game Launch** | Game loads in the iFrame (Web; Android/iOS via `--mobile`) | ✅ startup loop (`auto_handle_startup`) |
| 2 | **Default Bet Amount** | Default stake after launch matches expected | ✅ `--default-bet` check |
| 3 | **Minimum Bet** | Lowest selectable stake matches expected | ✅ `--min-bet` check (spam `bet -`) |
| 4 | **Wager recorded & deducted** | Stake leaves the wallet on spin (network/balance truth) | ✅ spin engine (`parse_result_body`, balance delta) |
| 5 | **Payout recorded & credited** | Win credited to wallet + shown in transaction summary | ✅ spin engine payout/balance; ⚠️ tx-summary screenshot only |
| 6 | **Feature triggered** | Bonus features (Free Spins, Pick, etc.) exist & trigger | ⚠️ feature detected in spin body + Buy-Bonus panel examined; full trigger not forced |
| 7 | **Record of main transaction** | Screenshot/clip of wager/payout/feature in tx summary | ✅ per-test screenshots + video clip |
| 8 | **Playthrough contribution** | Casino-bonus contribution % for the game | ⚠️ not automatable in-game — manual/reference |
| 9 | **Auto Play** | Autoplay starts, spins, and stops | ✅ `drive_autoplay` (network-verified, early-stop) |
| 10 | **Balance update** | Balance display updates after each round | ✅ pre/post balance read |
| 11 | **Transaction & Game History** | History panel present & populated (some games have none) | ✅ menu → History examined |
| 12 | **UI / iFrame** | Correct rendering, dimensions, animations — esp. mobile | ✅ fixed framing + control detection; `--mobile` |
| 13 | **Bet +/- buttons** | Stake changes via `-`/`+` | ✅ bet increment/decrement test |
| 14 | **Banking icon (amount selection)** | Coin/bank stake selector works | ⚠️ partial (bet display read) |
| 15 | **Full screen** | (JPC) custom fullscreen toggle works | ⚠️ control detected; toggle not yet exercised |
| 16 | **Audio** | Sound/music on-off works ("Split the Pot" has none) | ✅ sound toggle + menu Sound examined |

## Out of scope (not slots)
- **Opposite betting** — live casino only (Roulette / Sic Bo / Baccarat).
- **Side bets** — live & RNG **table** games only.
- **Crash auto-cashout**, **15% withholding tax (MZ all / ZM virtual)** — non-slot / regional wallet rules, handled outside the slot UI pass.
- **Game icon min-amount** — lobby tile, not in-game.

## Deferred
- **Abandon / Disconnection** — continue a round after leaving the page or killing the network during buy-feature/free-spins. Hard to drive reliably; revisit next iteration.

## Extra checks GameGuard already does (beyond the master list)
- **Detect ALL UI controls** — vision inventory of every interactive control (the annotated hero shot in the report).
- **Spin button behaviour** — fires a real spin, disabled during spin, re-enables after (network + motion truth).
- **Turbo / fast-spin** — control detected/operated.
- **Valid server response** — the spin endpoint returns well-formed JSON (TEST 10).
- **Agentic menu/paytable exploration** — opens the menu (or flat on-screen nav), examines each option, captures evidence; money/exit controls hard-blocked.
