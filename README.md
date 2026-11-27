# marketbot-research

Edge research for prediction-market strategies, run on GitHub Actions.

Everything here reads **public, unauthenticated** market data with HTTPS GET only. No API keys,
no account data, no orders. Each script refuses any host or path not on its own whitelist.

- `research/discovery/`: Kalshi edge screens, each started by editing its parameter file
  (e.g. `BTCLAST`), which triggers the matching workflow.
- `research/platforms/`: other venues (ForecastEx, Gemini, Polymarket, PredictIt, Crypto.com).
- `research/recorders/`: snapshots every 10 minutes of venues that keep no public price
  history (Gemini Predictions, Polymarket, PredictIt). Each UTC day is merged into one gzipped
  CSV per venue and attached to the release `data-YYYYMM`, so the data never enters git history.
- `research/fills/`: Kalshi order books every 20 minutes for the sports-longshot and temperature
  fill test (scored after 2026-12-09). Each run's export is an artifact `fill-books-<run id>`,
  kept 30 days. It stops itself after the window.

GitHub pauses scheduled workflows in a public repository after 60 days without a commit.

Test bar for any result: out of sample, confidence intervals clustered by day, both halves
agree, at least 30 losses, net of the venue's own fees.
