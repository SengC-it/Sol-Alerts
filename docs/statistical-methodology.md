# Statistical methodology

The ordered split is 60% train, 20% validation, and 20% untouched test. No random shuffle is used. The primary optimization metric is untouched-test mean R after fees, adverse slippage, funding when available, and the conservative impact proxy.

Adaptive evaluation uses a rolling 24-month long window and a recent 90-day window; older observations are stress-only. Dependent returns use deterministic contiguous block bootstrap resampling. Formal evidence requires a positive 95% lower bound for mean R, at least 30 untouched-test trades per symbol-family, and at least 300 untouched-test trades per family across the market. The portfolio maximum drawdown gate is 10%.

Challengers and champions are evaluated on identical untouched blocks. Publication requires a positive paired/block-bootstrap lower bound, overall improvement, no material recent degradation, drawdown compliance, and non-concentrated improvement. Weight changes are capped at 10 percentage points per weekly update. Symbol adjustments use sample-size shrinkage `n/(n+30)` and are capped at ±20 percentage points from the global base. Weight below 20% is shadow-only; recovery requires above 25% hysteresis.

The score is a deterministic 0–100 rank: historical robustness 30, current setup 30, market-state match 25, execution quality 15. A score is not a probability and never overrides a hard gate.

Historical eligibility uses a rolling 24-hour quote-volume sum and the frozen 20,000,000 USDT threshold. Historical bid/ask and order-book depth are not available in the public kline archive; formal preparation therefore records a conservative volatility/participation spread-depth proxy and refuses formal publication when that provenance is absent. Live scans still require the actual direction-side spread and 0.5% depth gates.

The live-equivalent research portfolio first combines same-direction family votes into one coherent activation, suppresses opposing directions, deduplicates sustained setups, applies invalidation and the one-hour re-arm cooldown, then applies 6/3/3 capacity on absolute timestamps. Raw family events remain diagnostic only; publication metrics, drawdown, concentration, cost stress, and Champion–Challenger decisions use the capacity-accepted events.
