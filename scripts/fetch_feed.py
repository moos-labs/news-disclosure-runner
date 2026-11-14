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

    if market_egx:
        # Arabic relevance guard: bare-code queries transliterate in
        # Arabic Google News (POUL -> بول "Paul"), flooding the feed
        # with unrelated people news. Arabic items must mention the
        # company's distinctive Arabic name token or the Latin code.
        alias_ar = (ALIASES.get(code) or {}).get("ar") or ""
        tokens = [w for w in re.split(r"\s+", alias_ar) if len(w) >= 5]
        if tokens:
            items = [i for i in items
                     if not is_arabic(i["title"])
                     or any(t in i["title"] for t in tokens)
                     or code in i["title"]]

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
        if not cik or not cik.isdigit():
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


def _title_tokens(t):
    return {w for w in re.split(r"[^\w]+", (t or "").lower())
            if len(w) >= 3}


def _item_day(it):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            return datetime.strptime(it.get("date", ""), fmt).date()
        except Exception:
            continue
    return None


def attach_egx_harvest(disc):
    """Issue #53: attach official EGX disclosure bodies harvested on
    the worker (issue #47) to disclosure items that came back
    title-only — Mubasher's pages are JS shells, so fetch_bodies
    cannot extract them, but the exchange's own text is now
    available locally via EGX_HARVEST_FILE. Matching: same company
    (the harvest is per-ticker) by title-token overlap, with a date
    agreement bonus. Returns how many bodies were attached."""
    path = os.environ.get("EGX_HARVEST_FILE")
    if not path or not os.path.exists(path):
        return 0
    try:
        harvest = json.load(open(path, encoding="utf-8"))
        h_items = [h for h in harvest.get("items", [])
                   if h.get("body") and len(h["body"]) >= 200]
    except Exception as e:
        print(f"WARN egx harvest unreadable: {e}", file=sys.stderr)
        return 0
    attached = 0
    for it in disc:
        if len(it.get("body") or "") >= 200:
            continue
        t_tok = _title_tokens(it.get("title"))
        if not t_tok:
            continue
        best, best_score = None, 0.0
        for h in h_items:
            h_tok = _title_tokens(h.get("title"))
            if not h_tok:
                continue
            inter = len(t_tok & h_tok)
            union = len(t_tok | h_tok) or 1
            score = inter / union
            h_day = None
            try:
                h_day = datetime.strptime(h.get("date", ""),
                                          "%Y-%m-%d").date()
            except Exception:
                pass
            i_day = _item_day(it)
            if h_day and i_day and h_day == i_day:
                score += 0.25
            if score > best_score:
                best, best_score = h, score
        if best is not None and best_score >= 0.35:
            it["body"] = best["body"][:6000]
            it["resolved_url"] = best.get("url", "")
            it["body_source"] = "egx-harvest"
            attached += 1
    if attached:
        print(f"egx-harvest: attached {attached} official bodies")
    return attached


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


def multilingual_scores(titles):
    """Average sentiment probabilities over ARABIC headlines with an
    XLM-RoBERTa multilingual model — FinBERT is English-only, and for
    EGX tickers most of the coverage is Arabic (in the first POUL pilot
    43 of 45 headlines were Arabic and went unscored). Labels are read
    from the model's own config, never assumed by index. Returns None
    if the model cannot load."""
    if not titles:
        return None
    try:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch
        name = "cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual"
        tok = AutoTokenizer.from_pretrained(name)
        mdl = AutoModelForSequenceClassification.from_pretrained(name)
        mdl.eval()
        id2label = {int(k): str(v).lower()
                    for k, v in (mdl.config.id2label or {}).items()}
        agg = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
        n = 0
        with torch.no_grad():
            for t in titles:
                enc = tok(t, return_tensors="pt", truncation=True,
                          max_length=128)
                probs = torch.softmax(mdl(**enc).logits, dim=1)[0].tolist()
                for i, p in enumerate(probs):
                    lab = id2label.get(i, "")
                    if "pos" in lab:
                        agg["positive"] += p
                    elif "neg" in lab:
                        agg["negative"] += p
                    else:
                        agg["neutral"] += p
                n += 1
        if n == 0:
            return None
        return {k: round(v / n, 3) for k, v in agg.items()} | {"count": n}
    except Exception as e:
        print(f"WARN multilingual sentiment unavailable: {e}",
              file=sys.stderr)
        return None


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 "
              "Safari/537.36"}


def resolve_gnews_url(url):
    """Google News RSS article links are opaque tokens behind a JS
    interstitial — there is no plain redirect to follow. Decode the
    token to the publisher URL through the page's own batchexecute
    endpoint (the technique RSS readers use). Returns None on any
    failure; non-Google links pass through unchanged."""
    if "news.google.com" not in url:
        return url
    try:
        page = requests.get(url, headers=BROWSER_UA, timeout=30).text
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        art = url.split("/articles/")[1].split("?")[0]
        if not (sg and ts):
            return None
        inner = ["Fbv4je",
                 '["garturlreq",[["X","X",["X","X"],null,null,1,1,'
                 '"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,'
                 '[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]'
                 % (art, ts.group(1), sg.group(1)),
                 None, "generic"]
        payload = "f.req=" + urllib.parse.quote(json.dumps([[inner]]))
        r = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute"
            "?rpcids=Fbv4je",
            headers={**BROWSER_UA,
                     "Content-Type":
                         "application/x-www-form-urlencoded;charset=UTF-8"},
            data=payload, timeout=30)
        txt = r.text.replace('\\"', '"')
        m = re.search(r'garturlres","(https?://[^"]+)"', txt)
        if not m:
            return None
        return (m.group(1).replace("\\u003d", "=")
                .replace("\\u0026", "&").replace("\\/", "/"))
    except Exception:
        return None


def _extract_body(html):
    """Extractor chain for an article page: trafilatura first, then
    readability, then the largest text block on the page. Returns the
    longest result any of them produced ('' if all failed)."""
    best = ""
    try:
        import trafilatura
        t = trafilatura.extract(html, favor_recall=True) or ""
        best = re.sub(r"\s+", " ", t).strip()
    except Exception:
        pass
    if len(best) < 200:
        try:
            from readability import Document
            from lxml import html as lxml_html
            # Untrusted markup: never resolve entities or touch the net.
            parser = lxml_html.HTMLParser(resolve_entities=False,
                                          no_network=True)
            frag = Document(html).summary()
            tree = lxml_html.fromstring(frag, parser=parser)
            t = re.sub(r"\s+", " ", tree.text_content()).strip()
            if len(t) > len(best):
                best = t
        except Exception:
            pass
    if len(best) < 200:
        try:
            from lxml import html as lxml_html
            parser = lxml_html.HTMLParser(resolve_entities=False,
                                          no_network=True)
            tree = lxml_html.fromstring(html, parser=parser)
            for bad in tree.iter("script", "style", "nav", "header",
                                 "footer", "aside", "form"):
                bad.getparent().remove(bad)
            blocks = []
            for el in tree.iter("article", "main", "div", "section"):
                t = re.sub(r"\s+", " ", el.text_content()).strip()
                if len(t) >= 200:
                    blocks.append(t)
            if blocks:
                t = max(blocks, key=len)
                if len(t) > len(best):
                    best = t
        except Exception:
            pass
    return best


def fetch_bodies(items, limit=12):
    """Fetch and extract the article BODY for the most important items
    (disclosures first, then most recent). Headlines are written to
    attract clicks — sentiment and event classification deserve the
    actual text when it is reachable. Every failure (paywall, block,
    extraction miss) leaves the item title-only; the run never depends
    on any single page. Bodies are capped for the sweep archive.
    Every failure is logged with its stage (decode / http / extract)
    so the yield can be diagnosed instead of guessed at."""
    ordered = sorted(items,
                     key=lambda i: (i.get("kind") == "disclosure",
                                    _pdate(i["date"])),
                     reverse=True)
    got = 0
    fails = {"decode": 0, "http": 0, "extract": 0}
    for it in ordered[:limit]:
        stage = "decode"
        try:
            real = None
            for _attempt in (1, 2):  # one decode retry (fresh signature)
                real = resolve_gnews_url(it["url"])
                if real:
                    break
            if not real:
                fails["decode"] += 1
                continue
            stage = "http"
            resp = requests.get(real, headers=BROWSER_UA, timeout=30,
                                stream=True)
            if resp.status_code != 200:
                fails["http"] += 1
                print(f"WARN body http {resp.status_code}: {real[:90]}",
                      file=sys.stderr)
                continue
            raw = b""
            for part in resp.iter_content(65536):  # streamed, capped
                raw += part
                if len(raw) > 3_000_000:
                    break
            html = raw.decode(resp.encoding or "utf-8", errors="replace")
            stage = "extract"
            text = _extract_body(html)
            if len(text) >= 200:
                it["body"] = text[:6000]
                it["resolved_url"] = real
                got += 1
            else:
                fails["extract"] += 1
                print(f"WARN body extract short ({len(text)} chars): "
                      f"{real[:90]}", file=sys.stderr)
        except Exception as e:
            fails[stage] += 1
            print(f"WARN body {stage} failed: {e}", file=sys.stderr)
            continue
    print(f"bodies: {got}/{min(limit, len(ordered))} fetched; "
          f"failures by stage: {fails}", file=sys.stderr)
    return got


# --- Translation chain: Arabic -> English --------------------------------
# THREE paths, tried in order (the same fallback discipline as the
# brief chain): 1) Cohere LLM — best quality, the key is already in
# this runner; 2) NLLB-200 distilled 600M — strongest local model;
# 3) Helsinki Opus-MT ar-en — lightest local model, most likely to
# load. Whatever serves is recorded in the summary; if all three fail,
# items stay Arabic and the multilingual leg still scores them.
#
# CHUNKED, per chunk fallback (fix for digest #25): the original
# design sent all 27 texts in ONE Cohere call and demanded an exact
# 27/27 numbered parse — Cohere answered, but only 24 lines parsed
# (run log: "parsed 24/27"), so the whole result was discarded and
# NLLB retranslated everything. Now texts go in chunks of 6; a chunk
# that fails a path falls to the next path ON ITS OWN, good chunks are
# never thrown away, and local models load once per run, not per chunk.

_MODEL_CACHE = {}


def _translate_cohere_chunk(texts):
    key = os.environ.get("COHERE_API_KEY")
    if not key or not texts:
        return None
    joined = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = ("Translate each numbered Arabic news text into English. "
              "Keep every number, percentage and name exact. Reply with "
              "the same numbers, one translation per line, nothing "
              f"else.\n\n{joined}")
    # Translation chunks are latency-bounded (issue #48): ONE attempt at
    # 30 s. In proof #45, three chunks burned ~5 minutes in repeated
    # 60 s timeouts before the local path — which serves a warm chunk in
    # ~15 s — was even tried. A fast failure beats a slow success here.
    for attempt in (1,):
        try:
            r = requests.post(
                "https://api.cohere.ai/compatibility/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": "command-r-08-2024",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max(600, 240 * len(texts)),
                      "temperature": 0.0},
                timeout=30)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"].strip()
            res = {}
            for line in out.splitlines():
                m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
                if m:
                    res[int(m.group(1)) - 1] = m.group(2).strip()
            if len(res) == len(texts):
                return [res[i] for i in range(len(texts))]
            print(f"WARN cohere translate: parsed {len(res)}/{len(texts)}",
                  file=sys.stderr)
            return None
        except Exception as e:
            print(f"WARN cohere translate attempt {attempt}: {e}",
                  file=sys.stderr)
    return None


def _get_nllb():
    if "nllb" not in _MODEL_CACHE:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        name = "facebook/nllb-200-distilled-600M"
        tok = AutoTokenizer.from_pretrained(name, src_lang="ara_Arab")
        mdl = AutoModelForSeq2SeqLM.from_pretrained(name)
        mdl.eval()
        _MODEL_CACHE["nllb"] = (tok, mdl)
    return _MODEL_CACHE["nllb"]


def _translate_nllb_chunk(texts):
    try:
        import torch
        tok, mdl = _get_nllb()
        bos = tok.convert_tokens_to_ids("eng_Latn")
        out = []
        with torch.no_grad():
            for t in texts:
                enc = tok(t, return_tensors="pt", truncation=True,
                          max_length=256)
                gen = mdl.generate(**enc, forced_bos_token_id=bos,
                                   max_length=256)
                out.append(tok.batch_decode(gen, skip_special_tokens=True)[0])
        return out
    except Exception as e:
        print(f"WARN nllb translate unavailable: {e}", file=sys.stderr)
        return None


def _get_opus():
    if "opus" not in _MODEL_CACHE:
        from transformers import MarianMTModel, MarianTokenizer
        name = "Helsinki-NLP/opus-mt-ar-en"
        tok = MarianTokenizer.from_pretrained(name)
        mdl = MarianMTModel.from_pretrained(name)
        mdl.eval()
        _MODEL_CACHE["opus"] = (tok, mdl)
    return _MODEL_CACHE["opus"]


def _translate_opus_chunk(texts):
    try:
        import torch
        tok, mdl = _get_opus()
        out = []
        with torch.no_grad():
            for t in texts:
                enc = tok([t], return_tensors="pt", truncation=True,
                          max_length=256, padding=True)
                gen = mdl.generate(**enc, max_length=256)
                out.append(tok.decode(gen[0], skip_special_tokens=True))
        return out
    except Exception as e:
        print(f"WARN opus translate unavailable: {e}", file=sys.stderr)
        return None


TRANSLATION_PATHS = [("cohere", _translate_cohere_chunk),
                     ("nllb", _translate_nllb_chunk),
                     ("opus-mt", _translate_opus_chunk)]


def translate_texts(texts, chunk_size=6, allow_llm=True):
    """Returns (translations, provider) — provider is the '+'-joined
    set of paths that actually served, in first-use order ('cohere'
    when the primary served everything); 'none' if any chunk failed
    on all three paths, in which case the caller keeps the items
    untranslated rather than misaligned. allow_llm=False (the
    --no-llm flag) removes the Cohere path entirely — the flag gates
    EVERY external LLM call, not just the brief."""
    paths = [(n, f) for n, f in TRANSLATION_PATHS
             if allow_llm or n != "cohere"]
    out, used = [], []
    for i in range(0, len(texts), chunk_size):
        chunk = texts[i:i + chunk_size]
        got, served = None, None
        for name, fn in paths:
            got = fn(chunk)
            if got and len(got) == len(chunk):
                served = name
                break
            got = None
        if got is None:
            return None, "none"
        out.extend(got)
        if served not in used:
            used.append(served)
    return out, "+".join(used)


def _pdate(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s or "", fmt)
            # strptime yields naive datetimes; the fallback below is
            # aware — never let the two mix in a sort (TypeError).
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
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
    lines = []
    for i in items[:10]:
        line = f"- [{i['date']}] ({i['source']}) {i['title']}"
        if i.get("body"):
            line += f"\n  Article text: {i['body'][:400]}"
        lines.append(line)
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
    ar_items = [i for i in (disc + news) if is_arabic(i["title"])]
    ar_titles = [i["title"] for i in ar_items]
    ar_count = len(ar_titles)
    sent = finbert_scores(en_titles)
    msent = multilingual_scores(ar_titles)

    # Depth over headlines: fetch article bodies for the top items,
    # translate the Arabic ones (3-path chain), and let the FINANCIAL
    # model score the translations — a general multilingual model
    # misreads financial Arabic ("profits fall 39%" came back
    # positive); FinBERT on a faithful translation does not.
    bodies_fetched = fetch_bodies(disc + news)
    egx_attached = attach_egx_harvest(disc)
    tr_texts = [(i["title"] + " — " + i["body"][:500]) if i.get("body")
                else i["title"] for i in ar_items]
    translations, tr_provider = (
        translate_texts(tr_texts, allow_llm=not args.no_llm)
        if tr_texts else (None, "none"))
    tr_sent = None
    if translations:
        for i, t in zip(ar_items, translations):
            i["text_en"] = t
        tr_sent = finbert_scores(translations)

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
    if msent:
        summary["multilingual_positive"] = msent["positive"]
        summary["multilingual_negative"] = msent["negative"]
        summary["multilingual_neutral"] = msent["neutral"]
        summary["multilingual_scored"] = msent["count"]
    summary["bodies_fetched"] = bodies_fetched
    summary["egx_bodies_attached"] = egx_attached
    summary["translation_provider"] = tr_provider
    if tr_sent:
        summary["translated_positive"] = tr_sent["positive"]
        summary["translated_negative"] = tr_sent["negative"]
        summary["translated_neutral"] = tr_sent["neutral"]
        summary["translated_scored"] = tr_sent["count"]
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
