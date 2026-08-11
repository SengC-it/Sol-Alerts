# Data limitations and survivorship controls

Binance public historical depth is not a complete long-horizon order-book archive. Research therefore uses a documented conservative volume/volatility/order-size impact proxy and base/double-slippage/impact stress scenarios. Live alerts still require actual current spread and cumulative depth.

Historical universe and liquidity membership must be reconstructed from information available at each timestamp. `sol-alerts-download` accepts a manifest made from dated official `exchangeInfo` snapshots and downloads every eligible active or delisted symbol; production research inputs must report missing-data gaps and fail closed on invalid periods. The smoke fixture is intentionally short and small; it is not formal evidence and cannot qualify an alert.

The repository does not store years of raw klines in Supabase. GitHub Actions may cache compressed inputs for research reproducibility, but workflows do not commit or publish complete raw market history. Official/formal backtests require a non-empty public funding file for every input symbol; a fixture's zero funding is explicitly not a live claim.
