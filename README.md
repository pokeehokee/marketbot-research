# marketbot-research

Edge research for prediction-market strategies, run on GitHub Actions.

Everything here reads **public, unauthenticated** market data with HTTPS GET only. No API keys,
no account data, no orders. Each script refuses any host or path not on its own whitelist.

- `research/discovery/`: Kalshi edge screens, each started by editing its parameter file
  (e.g. `BTCLAST`), which triggers the matching workflow.
- `research/platforms/`: other venues (ForecastEx, Gemini, Polymarket, PredictIt, Crypto.com).
- `research/recorders/`: scheduled snapshots of venues that keep no public price history.
  Their data is committed to the `data` branch, never to `main`.

Test bar for any result: out of sample, confidence intervals clustered by day, both halves
agree, at least 30 losses, net of the venue's own fees.
