#!/usr/bin/env python3
"""Fetch disclosures + news for a ticker from free sources, score headline
sentiment with FinBERT (no LLM), and write a short brief through an LLM
fallback chain (Cohere -> deterministic extractive brief).

Cost per run: $0. Fetching and sentiment use no LLM at all; the optional
brief is a single short call on a free-tier key, and the run still
produces a full result if every LLM is down.

Sources (all free, no keys):
- Google News RSS (Arabic + English locales). For EGX tickers this also
  surfaces Mubasher's republication of the official EGX disclosures
  (titles containing the ticker code / starting with the disclosure
  header), which is how EGX filings are classified here.
- SEC EDGAR submissions API (US tickers): official filings list.

Outputs in results/: news.json, disclosures.json, feed_summary.json,
headlines.txt
"""
import argparse, json, os, re, sys, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "Moos Labs research (moos@sherbeeny.com)"}

# Known EGX aliases; extend as the fleet covers more names. Generic
# code-based queries still run for tickers not listed here.
ALIASES = {
    "POUL.CA": {"ar": "القاهرة للدواجن", "en": "Cairo Poultry"},
    "COMI.CA": {"ar": "البنك التجاري الدولي", "en": "Commercial International Bank Egypt"},
    "ETEL.CA": {"ar": "المصرية للاتصالات", "en": "Telecom Egypt"},
    "HRHO.CA": {"ar": "إي إف جي هيرميس", "en": "EFG Hermes"},
    "TMGH.CA": {"ar": "طلعت مصطفى", "en": "Talaat Moustafa Group"},
    "SWDY.CA": {"ar": "السويدي إلكتريك", "en": "Elsewedy Electric"},
    "ESRS.CA": {"ar": "حديد عز", "en": "Ezz Steel"},
    "ABUK.CA": {"ar": "أبو قير للأسمدة", "en": "Abu Qir Fertilizers"},
    "AMOC.CA": {"ar": "الإسكندرية للزيوت المعدنية", "en": "AMOC Egypt"},
    "EKHO.CA": {"ar": "القابضة المصرية الكويتية", "en": "Egypt Kuwait Holding"},
}

MAJOR_FORMS = {"10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "S-1",
               "424B2", "FWP", "SC 13G", "SC 13D", "4"}


def is_arabic(text):
    return any("\u0600" <= ch <= "\u06FF" for ch in text or "")


def gnews(query, lang, country, days):
    q = f"{query} when:{days}d"
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
           + f"&hl={lang}&gl={country}&ceid={country}:{lang}")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        src = it.find("source")
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "date": it.findtext("pubDate") or "",
            "source": (src.text if src is not None else "").strip(),
            "url": it.findtext("link") or "",
        })
    return items


def classify_egx(item, code):
    """Mubasher republishes official EGX disclosures; their titles carry
    the ticker code and/or the disclosure header word."""
    t = item["title"]
    if code and code in t:
        return "disclosure"
    if t.startswith("العنوان"):
        return "disclosure"
    return "news"


def fetch_rss(ticker, days):
    market_egx = ticker.upper().endswith(".CA")
    code = ticker.upper()
    alias = ALIASES.get(code, {})
    queries = []
    if market_egx:
        if alias.get("ar"):
            queries.append((alias["ar"], "ar", "EG"))
        queries.append((code, "ar", "EG"))
        if alias.get("en"):
            queries.append((alias["en"], "en", "US"))
        queries.append((code + " stock", "en", "US"))
    else:
        bare = code.split(".")[0]
        queries.append((f"{bare} stock", "en", "US"))
        queries.append((f'"{bare}" earnings OR filing OR SEC', "en", "US"))
    seen, items = set(), []
    for q, lang, country in queries:
        try:
            got = gnews(q, lang, country, days)
        except Exception as e:
            print(f"WARN gnews failed ({q}): {e}", file=sys.stderr)
            continue
        for it in got:
            key = re.sub(r"\s+", " ",
                         re.sub(r" - [^-]+$", "", it["title"])).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            it["kind"] = (classify_egx(it, code) if market_egx else "news")
            items.append(it)

    def sortkey(it):
        try:
            return datetime.strptime(it["date"], "%a, %d %b %Y %H:%M:%S %Z")
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    items.sort(key=sortkey, reverse=True)
    return items


def fetch_edgar(ticker):
    bare = ticker.upper().split(".")[0]
    try:
        m = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=UA, timeout=30).json()
        cik = None
        for row in m.values():
            if row["ticker"] == bare:
                cik = str(row["cik_str"]).zfill(10)
                break
        if not cik:
            return []
        d = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=UA, timeout=30).json()
        rec = d["filings"]["recent"]
        out = []
        for i in range(len(rec["form"])):
            if rec["form"][i] not in MAJOR_FORMS:
                continue
            acc = rec["accessionNumber"][i].replace("-", "")
            out.append({
                "title": f"{rec['form'][i]} — {d.get('name', bare)}",
                "date": rec["filingDate"][i],
                "source": "SEC EDGAR",
                "url": (f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(cik)}/{acc}/{rec['primaryDocument'][i]}"),
                "kind": "disclosure",
                "form": rec["form"][i],
            })
            if len(out) >= 25:
                break
        return out
    except Exception as e:
        print(f"WARN edgar failed: {e}", file=sys.stderr)
        return []


def finbert_scores(titles):
    """Average FinBERT probabilities over English headlines. Returns None
    if the model cannot load — sentiment is an enhancement, not a gate."""
    try:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch
        tok = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        mdl = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        mdl.eval()
        agg = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
        n = 0
        with torch.no_grad():
            for t in titles:
                enc = tok(t, return_tensors="pt", truncation=True, max_length=128)
                probs = torch.softmax(mdl(**enc).logits, dim=1)[0].tolist()
                agg["positive"] += probs[0]
                agg["negative"] += probs[1]
                agg["neutral"] += probs[2]
                n += 1
        if n == 0:
            return None
        return {k: round(v / n, 3) for k, v in agg.items()} | {"count": n}
    except Exception as e:
        print(f"WARN finbert unavailable: {e}", file=sys.stderr)
        return None


def _pdate(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            return datetime.strptime(s or "", fmt)
        except Exception:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


def by_recency(items):
    return sorted(items, key=lambda i: _pdate(i["date"]), reverse=True)


def llm_brief(items):
    """One short call through the fallback chain. Returns (text, provider).
    Chain: Cohere (org key, one retry on timeout) -> none (extractive
    fallback by caller). Every attempt is time-boxed so a slow provider
    can never stall the run."""
    key = os.environ.get("COHERE_API_KEY")
    if not key:
        return None, "none"
    items = by_recency(items)
    lines = [f"- [{i['date']}] ({i['source']}) {i['title']}" for i in items[:10]]
    prompt = (
        "You are a market-news briefer. From these headlines and disclosure "
        "titles (Arabic and English), write a brief of at most 8 short "
        "bullets: what happened, any hard numbers mentioned, and why it "
        "matters for the stock. Use only what the headlines say; do not "
        "invent figures. Reply in English.\n\n" + "\n".join(lines))
    for attempt in (1, 2):
        try:
            r = requests.post(
                "https://api.cohere.ai/compatibility/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": "command-r-08-2024",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 350, "temperature": 0.2},
                timeout=60)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            return text, "cohere"
        except Exception as e:
            print(f"WARN cohere brief attempt {attempt} failed: {e}",
                  file=sys.stderr)
    return None, "none"


def extractive_brief(news, disc):
    lines = ["Extractive brief (no LLM used — top items by recency):"]
    for i in by_recency(disc + news)[:8]:
        lines.append(f"- [{i['date']}] ({i['source']}) {i['title']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-news", type=int, default=40)
    ap.add_argument("--max-disc", type=int, default=25)
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    ticker = args.ticker.upper()
    os.makedirs("results", exist_ok=True)

    items = fetch_rss(ticker, args.days)
    disc = [i for i in items if i["kind"] == "disclosure"]
    news = [i for i in items if i["kind"] != "disclosure"]
    if not ticker.endswith(".CA"):
        edgar = fetch_edgar(ticker)
        disc = edgar + disc
    disc, news = disc[:args.max_disc], news[:args.max_news]
    if not disc and not news:
        print("ERROR: no items fetched from any source", file=sys.stderr)
        return 1

    en_titles = [i["title"] for i in (disc + news) if not is_arabic(i["title"])]
    ar_count = sum(1 for i in (disc + news) if is_arabic(i["title"]))
    sent = finbert_scores(en_titles)

    if args.no_llm:
        brief, provider = None, "disabled"
    else:
        brief, provider = llm_brief(disc + news)
    if not brief:
        brief = extractive_brief(news, disc)
        if provider == "none":
            provider = "extractive"

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open("results/news.json", "w", encoding="utf-8") as f:
        json.dump({"ticker": ticker, "fetched_at": stamp, "items": news},
                  f, ensure_ascii=False, indent=1)
    with open("results/disclosures.json", "w", encoding="utf-8") as f:
        json.dump({"ticker": ticker, "fetched_at": stamp, "items": disc},
                  f, ensure_ascii=False, indent=1)
    summary = {
        "tool": "news-disclosure",
        "ticker": ticker,
        "news_count": len(news),
        "disclosure_count": len(disc),
        "arabic_headlines": ar_count,
        "latest_news_date": news[0]["date"] if news else "",
        "latest_disclosure_date": disc[0]["date"] if disc else "",
        "top_headline": (disc + news)[0]["title"] if (disc + news) else "",
        "llm_provider": provider,
        "llm_summary": brief,
    }
    if sent:
        summary["finbert_positive"] = sent["positive"]
        summary["finbert_negative"] = sent["negative"]
        summary["finbert_neutral"] = sent["neutral"]
        summary["finbert_scored"] = sent["count"]
    with open("results/feed_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open("results/headlines.txt", "w", encoding="utf-8") as f:
        for i in disc + news:
            f.write(f"[{i['kind']}] [{i['date']}] ({i['source']}) {i['title']}\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "llm_summary"},
                     ensure_ascii=False))
    print("BRIEF:\n" + brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
