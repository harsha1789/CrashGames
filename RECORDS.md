# DSC bet records (`runs/DSC_Report_*_records.jsonl`)

Every DSC spin appends ONE JSON line next to the Excel report. This is the input for the
phase-2 **transaction-history validation** (`tlogs_validate.py`): Betway reflects bets in
~5 minutes, so shortly after the sweep run

    python tlogs_validate.py --records runs/DSC_Report_<ts>_records.jsonl [--headed]

or use the dashboard's **Validate transactions** link (also on the batch-complete card).
It logs into the brand site with each worker account (Playwright), scrapes Transaction
History, reconciles every record by the rules below, and writes
`…_validation.json` next to the records file. Site selectors per region live in
`SITES[(brand, region)]` in tlogs_validate.py — ZA is implemented.

## Fields

| Field | Meaning |
|---|---|
| `recorded_at` | When the record was written (ISO, local tz) |
| `spin_at` | Client-side moment the spin was clicked (ISO, local tz) |
| `server_time` | **Wallet server timestamp of the transaction** (RagingRiver: `"2026-07-10 09:25:53"` UTC; Bugatti: `"7/10/2026 9:17:20 AM"` UTC). Prefer this over `spin_at` for matching |
| `brand` / `region` / `account` | Worker identity — history is per account, always filter by it first |
| `srNo` / `provider` / `game` | Sheet row identity. NB: history may show the aggregator (e.g. "RagingRiver"), not the studio ("Red Tiger") — match provider via `endpoint`, not this column |
| `launch` / `bet_placed` / `tlogs` | The three report verdicts (bools) |
| `wager` | Wager of record (balance-delta effective — trust this one) |
| `wager_response` | Wager as claimed by the provider response (may disagree; both kept) |
| `payout` | Payout of record |
| `balance_before` / `balance_after` | Pre-/post-spin balance as observed (network when parseable, else vision) |
| `balance_at_start` | **Exact** wallet cash before the bet (wire, when provider exposes it) |
| `balance_after_bet` | **Exact** cash after the wager deduction — this equals the parenthetical running balance printed in transaction history next to the wager line |
| `balance_at_end` | **Exact** cash after payout settlement (= `balance_after_bet` + `payout`) |
| `round_id` | Provider platform transaction/round id (RagingRiver `transactions.roundId`) — the handle for back-office/Tlogs lookup |
| `tnum` | Bugatti-family sequential transaction number |
| `source` | `network` (exact wire values) or `visual` (OCR fallback) |
| `endpoint` | The spin endpoint path (e.g. `/evo-ragingrivermga00/platform/game/spin`, `/bugatti/play`) |
| `non_slot` | Reason string when the game was classified not-a-slot (bet flow skipped) |
| `errors` | Everything abnormal, verbatim (cap refusals, mislabeled steppers, min-bet mismatches) |
| `evidence` | Run folder with screenshots/video for this game |

Missing/unavailable values are `null` — the keys are always present (stable schema).

## How to match a record to a transaction-history entry

History entry shape: `RagingRiver Wager · R -1.60 (144.41) · <wallet GUID>` at minute resolution.

1. Filter history to the record's `account`.
2. Time window: `server_time` ±2 min (fall back to `spin_at` ±3 min when `server_time` is null).
3. Amount: history amount == `wager` (±0.01).
4. Clincher: history's parenthetical running balance == `balance_after_bet`
   (fallback: `balance_before − wager`). This disambiguates same-amount spins in the
   same minute — the running balance is unique per entry.
5. If `payout > 0`, also expect a paired Win entry: `+payout`, running balance `balance_at_end`.
6. The wallet GUID shown in history is generated wallet-side and never appears in game
   traffic — `round_id` / `tnum` are the ids to quote when escalating to the back office.

**Absence checks (both directions):** a record with `bet_placed: false` must have NO
matching history entry; any history entry in the sweep window matching NO record is an
unaccounted wager and should fail the reconciliation loudly.
