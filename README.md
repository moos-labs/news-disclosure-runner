# news-disclosure-runner

Fetches **disclosures + news** for a ticker from free sources — the layer
the rest of the Borsagator fleet was missing — and writes a short brief.

## Cost: $0 per run

- Fetching: Google News RSS + SEC EDGAR — free, no API keys.
- Sentiment: FinBERT runs locally in the runner (no LLM).
- Brief: one short call on the fleet's free-tier Cohere key, with a
  fallback chain — if the LLM is down or unkeyed, a deterministic
  extractive brief is produced instead. The run never fails over an LLM.

## Sources

- **EGX (.CA tickers):** Google News RSS in Arabic + English. Mubasher
  republishes the official EGX disclosures and their titles carry the
  ticker code, so they are classified as `disclosure` items; financial
  press (Al Mal, Al Borsa, Youm7, Amwal Alghad…) lands as `news`.
- **US tickers:** SEC EDGAR submissions API (official filings: 10-K,
  10-Q, 8-K, 20-F, 6-K…) + Google News RSS.

## Run

Driven by the `COMMANDS` Actions variable, like every fleet runner:

```
set -o pipefail; mkdir -p results; python scripts/fetch_feed.py --ticker POUL.CA 2>&1 | tee results/run.log
```

Outputs in `results/`: `news.json`, `disclosures.json`,
`feed_summary.json` (counts, FinBERT shares, brief + which provider
wrote it), `headlines.txt`.

Known EGX company aliases live in `ALIASES` in
`scripts/fetch_feed.py` — extend the map as coverage grows; tickers
without an alias still get code-based Arabic + English queries.
