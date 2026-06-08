"""
Stock Watchlist - Local Data Server
Serves watchlist.html and fetches Japanese stock data from Yahoo Finance.

Usage:
  python server.py
  Then open http://localhost:5000 in your browser.

Requirements:
  pip install yfinance requests anthropic

Environment variables:
  ANTHROPIC_API_KEY  - Claude APIキー（/api/explain で使用、未設定なら機能無効）
"""

import json
import os
import time
import requests as req_lib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import yfinance as yf

PORT               = 5000
HERE               = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE     = os.path.join(HERE, "watchlist.json")
MARKET_BRIEF_FILE  = os.path.join(HERE, "market_brief.json")
MARKET_BRIEF_TTL   = 8 * 3600  # 8時間でキャッシュ切れ（秒）

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ── Anthropic クライアント（遅延初期化） ──────────────
_anthropic_client = None
def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    _anthropic_client = Anthropic(api_key=api_key)
    return _anthropic_client

# 解説のサーバー側キャッシュ（コード+カテゴリ+スコアでキー）
_explain_cache = {}

# 会社概要のサーバー側キャッシュ（コード+市場でキー）
_profile_cache = {}


class StockHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        # ── HTML を配信 ──────────────────────────────
        if path in ("/", "/watchlist.html", "/index.html"):
            self._send_file("watchlist.html", "text/html; charset=utf-8")
            return

        # ── ヘルスチェック ────────────────────────────
        if path == "/health":
            self._send_json({"status": "ok"})
            return

        # ── ウォッチリスト読み込み GET /api/watchlist ──
        if path == "/api/watchlist":
            try:
                if os.path.exists(WATCHLIST_FILE):
                    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                else:
                    data = []
                self._send_json(data)
            except Exception as e:
                print(f"  [ERROR] read watchlist: {e}")
                self._send_json([])
            return

        # ── 株価取得 /api/quote?code=7203&market=jp ──────
        if path == "/api/quote":
            params = parse_qs(parsed.query)
            code   = params.get("code",   [""])[0].strip().upper()
            market = params.get("market", ["jp"])[0].strip().lower()
            if not code:
                self._send_json({"error": "code parameter required"}, 400)
                return
            data = self._fetch_quote(code, market)
            if data is None:
                self._send_json({"error": f"Failed to fetch {code}"}, 500)
            else:
                self._send_json(data)
            return

        # ── 会社概要 /api/profile?code=7203&market=jp ──────────
        if path == "/api/profile":
            params = parse_qs(parsed.query)
            code   = params.get("code",   [""])[0].strip().upper()
            market = params.get("market", ["jp"])[0].strip().lower()
            if not code:
                self._send_json({"error": "code parameter required"}, 400)
                return
            data = self._fetch_profile(code, market)
            self._send_json(data)
            return

        # ── ニュース取得 /api/news?market=jp&code=7203 ──────────
        if path == "/api/news":
            params = parse_qs(parsed.query)
            market = params.get("market", ["jp"])[0].strip().lower()
            code   = params.get("code",   [""])[0].strip().upper()
            data   = self._fetch_news(code, market)
            self._send_json(data)
            return

        # ── マーケットブリーフィング /api/market-brief ────────
        # ?refresh=1 で強制再取得、それ以外はキャッシュ（8時間）を使用
        if path == "/api/market-brief":
            params = parse_qs(parsed.query)
            force  = params.get("refresh", [""])[0] == "1"
            if not force and os.path.exists(MARKET_BRIEF_FILE):
                try:
                    if time.time() - os.path.getmtime(MARKET_BRIEF_FILE) < MARKET_BRIEF_TTL:
                        with open(MARKET_BRIEF_FILE, "r", encoding="utf-8") as f:
                            self._send_json(json.load(f))
                        return
                except Exception:
                    pass
            try:
                brief = self._fetch_market_brief()
                self._send_json(brief)
            except Exception as e:
                print(f"  [ERROR] market-brief: {e}")
                self._send_json({"available": False, "error": str(e)}, 500)
            return

        # ── ヒストリカルデータ /api/history?code=7203&market=jp&period=6mo ─
        if path == "/api/history":
            params = parse_qs(parsed.query)
            code   = params.get("code",   [""])[0].strip().upper()
            market = params.get("market", ["jp"])[0].strip().lower()
            period = params.get("period", ["1d"])[0].strip()
            interval_map = {
                "1d":  "5m",   # 1日：5分足
                "5d":  "30m",  # 5日：30分足
                "1mo": "1d",
                "3mo": "1d",
                "6mo": "1d",
                "1y":  "1d",
                "5y":  "1wk",
            }
            if period not in interval_map:
                period = "1d"
            interval = interval_map[period]
            ticker_str = self._ticker(code, market)
            try:
                import math
                intraday = interval in ("5m", "15m", "30m", "60m", "1h")
                t    = yf.Ticker(ticker_str)
                hist = t.history(period=period, interval=interval)
                result = []
                for date, row in hist.iterrows():
                    o, h, l, c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
                    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in [o, h, l, c]):
                        continue
                    vol = row.get("Volume", 0)
                    # イントラデイは Unixタイムスタンプ（秒）、日次以上は YYYY-MM-DD 文字列
                    if intraday:
                        try:
                            time_val = int(date.timestamp())
                        except Exception:
                            time_val = int(date.value // 1_000_000_000)
                    else:
                        time_val = date.strftime("%Y-%m-%d")
                    result.append({
                        "time":     time_val,
                        "open":     round(float(o), 4),
                        "high":     round(float(h), 4),
                        "low":      round(float(l), 4),
                        "close":    round(float(c), 4),
                        "volume":   int(vol) if vol and not math.isnan(vol) else 0,
                        "intraday": intraday,
                    })
                self._send_json(result)
            except Exception as e:
                print(f"  [ERROR] history {code}: {e}")
                self._send_json({"error": str(e)}, 500)
            return

        # ── 関連銘柄取得 /api/related?code=7203&market=jp ─
        if path == "/api/related":
            params = parse_qs(parsed.query)
            code   = params.get("code",   [""])[0].strip().upper()
            market = params.get("market", ["jp"])[0].strip().lower()
            if not code:
                self._send_json({"error": "code parameter required"}, 400)
                return
            data = self._fetch_related(code, market)
            self._send_json(data)
            return

        # ── スクリーニング /api/screen?market=jp&category=dividend&exclude=7203,6758 ─
        if path == "/api/screen":
            params   = parse_qs(parsed.query)
            market   = params.get("market",   ["jp"])[0].strip().lower()
            category = params.get("category", ["other"])[0].strip()
            exclude  = [c.strip().upper() for c in params.get("exclude", [""])[0].split(",") if c.strip()]
            data = self._screen_stocks(market, category, exclude)
            self._send_json(data)
            return

        self._send_json({"error": "Not found"}, 404)

    # ── ウォッチリスト保存 POST /api/watchlist ────────
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/watchlist":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                data   = json.loads(body)
                with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._send_json({"status": "ok"})
                print(f"  Watchlist saved: {len(data)} items -> {WATCHLIST_FILE}")
            except Exception as e:
                print(f"  [ERROR] save watchlist: {e}")
                self._send_json({"error": str(e)}, 500)
            return

        # ── スコア解説 POST /api/explain ──────────────
        if parsed.path == "/api/explain":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length))
                text   = self._explain_score(body)
                if text is None:
                    self._send_json({"error": "Claude APIキーが未設定です（環境変数 ANTHROPIC_API_KEY）"}, 503)
                else:
                    self._send_json({"explanation": text})
            except Exception as e:
                print(f"  [ERROR] explain: {e}")
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    # ── Claude でスコアを解説 ─────────────────────────
    def _explain_score(self, body):
        client = _get_anthropic()
        if client is None:
            return None

        code         = body.get("code", "")
        name         = body.get("name", code)
        category     = body.get("category", "other")
        news_context = body.get("newsContext", "")  # 市場ニュースのサマリー（任意）
        score    = body.get("score")
        detail   = body.get("detail", {}) or {}
        price    = body.get("price")
        currency = body.get("currency", "JPY")
        per      = body.get("per")
        pbr      = body.get("pbr")
        div      = body.get("div")
        pos52    = body.get("pos52")
        revG     = body.get("revenueGrowth")
        gm       = body.get("grossMargins")
        cap      = body.get("marketCap")

        cache_key = f"{code}|{category}|{score}"
        if cache_key in _explain_cache:
            return _explain_cache[cache_key]

        cat_labels = {
            "undervalue": "💎 割安優良株（PER/PBR重視）",
            "tech":       "🚀 先進技術（成長性重視）",
            "dividend":   "💰 安定配当（配当利回り重視）",
            "ai":         "🤖 AI半導体（将来性重視）",
            "tenx":       "💥 10倍候補（売上成長・小型・高粗利重視）",
            "other":      "📌 その他",
        }
        cat_label = cat_labels.get(category, category)

        # 指標の文字列化
        metric_lines = []
        if per   is not None: metric_lines.append(f"・PER: {per:.1f} 倍")
        if pbr   is not None: metric_lines.append(f"・PBR: {pbr:.2f} 倍")
        if div   is not None: metric_lines.append(f"・配当利回り: {div*100:.2f}%")
        if pos52 is not None: metric_lines.append(f"・52週レンジ位置: {pos52:.0f}%（0=安値、100=高値）")
        if revG  is not None: metric_lines.append(f"・売上成長率: {revG*100:.1f}%")
        if gm    is not None: metric_lines.append(f"・粗利率: {gm*100:.1f}%")
        if cap   is not None:
            cap_str = f"${cap/1e9:.1f}B" if currency == "USD" else (
                f"{cap/1e12:.2f}兆円" if cap >= 1e12 else f"{cap/1e8:.0f}億円")
            metric_lines.append(f"・時価総額: {cap_str}")

        detail_lines = []
        det_labels = {"per":"PER","pbr":"PBR","div":"配当","pos":"52週位置",
                      "revG":"売上成長","cap":"時価総額","gm":"粗利率"}
        for k, v in detail.items():
            detail_lines.append(f"・{det_labels.get(k, k)}: {v}/25点")

        price_str = ""
        if price is not None:
            price_str = f"${price:.2f}" if currency == "USD" else f"{price:,.1f}円"

        news_section = f"\n【本日のマーケットニュース】\n{news_context}\n" if news_context else ""

        prompt = f"""以下は株式の買いタイミング判定結果です。スコアの根拠と注目ポイントを、日本語の口語で**3行以内**で簡潔に説明してください。

【絶対ルール】
- このユーザーは「超長期・買いのみ」の投資方針。**売り推奨は絶対にしない**こと。
- 「いつ買うか」の観点で語ること。
- 投資助言ではなく、データ解釈の補助情報として書くこと。

【銘柄】 {name}（{code}）
【カテゴリ】 {cat_label}
【現在値】 {price_str}
【総合スコア】 {score}/100点

【指標値】
{chr(10).join(metric_lines) or "（データ不足）"}

【スコア内訳（25点満点）】
{chr(10).join(detail_lines) or "（なし）"}{news_section}
3行以内で：
1行目：この銘柄の現状を一言で
2行目：スコアの主な根拠（高/低い要因）{"＋今日のニュースとの関連があれば言及" if news_context else ""}
3行目：このカテゴリ・NISA長期投資の観点での買い場判断（"様子見" / "押し目待ち" / "良い買い場" など）"""

        try:
            msg = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            _explain_cache[cache_key] = text
            return text
        except Exception as e:
            print(f"  [ERROR] anthropic: {e}")
            return f"⚠️ Claude API 呼び出しエラー: {e}"

    # ── マーケットブリーフィング生成 ─────────────────────
    def _fetch_market_brief(self):
        """yfinanceで主要指数・為替を取得し market_brief.json を生成・返す。
        Coworkスケジュールタスクに依存せず、サーバー自身がデータを取得する。
        結果は MARKET_BRIEF_FILE にキャッシュされ TTL 内は再取得しない。
        """
        import datetime
        import re

        now_jst = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=9))
        ).strftime("%Y-%m-%d %H:%M（JST）")

        def get_index(ticker_str):
            try:
                t     = yf.Ticker(ticker_str)
                fi    = t.fast_info
                price = self._num(fi.last_price)
                prev  = self._num(fi.previous_close)
                chg   = ((price - prev) / prev * 100) if (price and prev) else None
                return {
                    "value":      round(price, 2) if price is not None else None,
                    "change_pct": round(chg,   2) if chg   is not None else None,
                }
            except Exception as e:
                print(f"  [WARN] market-brief index {ticker_str}: {e}")
                return {"value": None, "change_pct": None}

        def get_fx(ticker_str):
            try:
                t  = yf.Ticker(ticker_str)
                fi = t.fast_info
                v  = self._num(fi.last_price)
                return round(v, 2) if v is not None else None
            except Exception as e:
                print(f"  [WARN] market-brief fx {ticker_str}: {e}")
                return None

        print("  [INFO] Fetching market brief from yfinance...")
        nikkei = get_index("^N225")
        topix  = get_index("^TOPIX")
        dow    = get_index("^DJI")
        sp500  = get_index("^GSPC")
        nasdaq = get_index("^IXIC")
        usdjpy = get_fx("JPY=X")
        eurjpy = get_fx("EURJPY=X")

        news_items = self._fetch_news("", "jp")
        highlights = [n["title"] for n in news_items[:5]]

        brief = {
            "generated_at": now_jst,
            "available":    True,
            "indices": {
                "nikkei": nikkei,
                "topix":  topix,
                "dow":    dow,
                "sp500":  sp500,
                "nasdaq": nasdaq,
            },
            "fx": {
                "usdjpy": usdjpy,
                "eurjpy": eurjpy,
            },
            "highlights":   highlights,
            "hot_stocks":   [],
            "sentiment":    "",
            "news_summary": "",
        }

        client = _get_anthropic()
        if client:
            try:
                def fmt(d):
                    v = d.get("value")
                    c = d.get("change_pct")
                    return f"{v}({c:+.2f}%)" if (v and c is not None) else "N/A"

                idx_text = (
                    f"日経平均{fmt(nikkei)}, TOPIX{fmt(topix)}, "
                    f"ダウ{fmt(dow)}, S&P500{fmt(sp500)}, "
                    f"ドル円{usdjpy}, ユーロ円{eurjpy}"
                )
                hl_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(highlights[:5])) \
                          or "（ニュース取得なし）"
                prompt = (
                    "以下のマーケットデータを元に、次の3つを日本語で生成してください。\n"
                    "- sentiment: マーケットの雰囲気を20文字以内の一言で\n"
                    "- news_summary: ニュース全体を2〜3文で要約\n"
                    "- highlights_jp: 各ニュースを15〜30文字の日本語見出しに要約した配列"
                    "（入力ニュースと同じ件数・同じ順序で返すこと）\n\n"
                    "出力は次の形式のJSONのみ。他の文字は一切含めないこと:\n"
                    '{"sentiment": "...", "news_summary": "...", "highlights_jp": ["...", "..."]}\n\n'
                    f"指数: {idx_text}\nニュース:\n{hl_text}"
                )
                msg = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=600,
                    messages=[{"role": "user", "content": prompt}],
                )
                m = re.search(r'\{.*\}', msg.content[0].text, re.DOTALL)
                if m:
                    parsed = json.loads(m.group())
                    brief["sentiment"]     = parsed.get("sentiment", "")
                    brief["news_summary"]  = parsed.get("news_summary", "")
                    jp_titles              = parsed.get("highlights_jp") or []
                    # 日本語版が取れた件数だけ置き換え（順序・件数が合えば元タイトルを丸ごと差し替え）
                    if isinstance(jp_titles, list) and jp_titles:
                        brief["highlights"] = [
                            jp_titles[i] if i < len(jp_titles) and jp_titles[i] else h
                            for i, h in enumerate(highlights)
                        ]
            except Exception as e:
                print(f"  [WARN] market-brief sentiment gen: {e}")

        try:
            with open(MARKET_BRIEF_FILE, "w", encoding="utf-8") as f:
                json.dump(brief, f, ensure_ascii=False, indent=2)
            print(f"  [INFO] market_brief.json saved -> {MARKET_BRIEF_FILE}")
        except Exception as e:
            print(f"  [WARN] market_brief.json save failed: {e}")

        return brief

    # ── 会社概要取得 ──────────────────────────────────
    def _fetch_profile(self, code, market="jp"):
        """会社概要（事業内容・セクター・従業員数など）を取得。
        日本企業は yfinance の英語要約を Claude で日本語に翻訳・要約する。
        結果は in-memory にキャッシュ（プロセス再起動でクリア）。
        """
        cache_key = f"{code}|{market}"
        if cache_key in _profile_cache:
            return _profile_cache[cache_key]

        ticker_str = self._ticker(code, market)
        try:
            t    = yf.Ticker(ticker_str)
            info = t.info or {}
        except Exception as e:
            print(f"  [ERROR] _fetch_profile {code}: {e}")
            return {"available": False, "error": str(e)}

        summary = info.get("longBusinessSummary") or ""
        profile = {
            "available":   True,
            "name":        info.get("longName") or info.get("shortName") or code,
            "sector":      info.get("sector", "") or "",
            "industry":    info.get("industry", "") or "",
            "summary":     summary,
            "summary_jp":  "",
            "website":     info.get("website", "") or "",
            "employees":   info.get("fullTimeEmployees"),
            "country":     info.get("country", "") or "",
            "city":        info.get("city", "") or "",
            "founded":     info.get("yearFounded") if isinstance(info.get("yearFounded"), int) else None,
            "exchange":    info.get("exchange", "") or "",
            "currency":    "USD" if market == "us" else "JPY",
            "market":      market,
        }

        # 日本企業（または英文要約のある銘柄）は Claude で日本語要約を生成
        if summary and (market == "jp" or len(summary) > 200):
            client = _get_anthropic()
            if client:
                try:
                    prompt = (
                        f"次の{'日本' if market == 'jp' else ''}企業の事業内容を、"
                        "日本語で180〜260文字程度に要約してください。"
                        "主力事業・強み・特徴が分かるように、投資判断の補助情報として書いてください。\n"
                        "前置きや見出しは不要。要約本文のみ返してください。\n\n"
                        f"会社名: {profile['name']}\n"
                        f"セクター: {profile['sector']}\n"
                        f"業界: {profile['industry']}\n\n"
                        f"原文（英語）:\n{summary[:1800]}"
                    )
                    msg = client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=500,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    profile["summary_jp"] = msg.content[0].text.strip()
                except Exception as e:
                    print(f"  [WARN] profile JP summary {code}: {e}")

        _profile_cache[cache_key] = profile
        return profile

    # ── ニュース取得 ──────────────────────────────────
    def _fetch_news(self, code="", market="jp"):
        """yfinanceで日経225・指定銘柄のニュースを取得"""
        results = []
        tickers = ["^N225", "^GSPC"] if market == "jp" else ["^GSPC", "^DJI"]
        if code:
            tickers.insert(0, self._ticker(code, market))

        seen = set()
        for ticker_str in tickers:
            try:
                t = yf.Ticker(ticker_str)
                news = t.news or []
                for item in news[:8]:
                    content = item.get("content", {})
                    title = content.get("title") or item.get("title", "")
                    url   = content.get("canonicalUrl", {}).get("url") or item.get("link", "")
                    pub   = content.get("pubDate") or content.get("displayTime") or ""
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    results.append({
                        "title":     title,
                        "url":       url,
                        "published": pub,
                        "source":    ticker_str,
                    })
            except Exception as e:
                print(f"  [WARN] news fetch {ticker_str}: {e}")

        return results[:12]

    # ── ティッカー文字列を生成 ────────────────────────
    def _ticker(self, code, market):
        if market == "jp":
            return code + ".T"
        # us (NASDAQ/NYSE) はそのまま
        return code

    # ── 安全な数値キャスト（yfinance が時々文字列を返すので） ──
    @staticmethod
    def _num(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f

    # ── 株価データ取得 ─────────────────────────────────
    def _fetch_quote(self, code, market="jp"):
        ticker_str = self._ticker(code, market)
        try:
            t    = yf.Ticker(ticker_str)
            info = t.info

            try:
                fi     = t.fast_info
                price  = self._num(fi.last_price)
                high52 = self._num(fi.year_high)
                low52  = self._num(fi.year_low)
                cap    = self._num(fi.market_cap)
            except Exception:
                price  = self._num(info.get("regularMarketPrice") or info.get("currentPrice"))
                high52 = self._num(info.get("fiftyTwoWeekHigh"))
                low52  = self._num(info.get("fiftyTwoWeekLow"))
                cap    = self._num(info.get("marketCap"))

            prev   = self._num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
            change = (price - prev)        if (price is not None and prev is not None) else None
            chgpct = (change / prev * 100) if (change is not None and prev)             else None

            currency = "USD" if market == "us" else "JPY"
            return {
                "shortName":                  info.get("shortName") or info.get("longName") or code,
                "longName":                   info.get("longName") or code,
                "regularMarketPrice":         price,
                "regularMarketChange":        change,
                "regularMarketChangePercent": chgpct,
                "fiftyTwoWeekHigh":           high52,
                "fiftyTwoWeekLow":            low52,
                "marketCap":                  cap,
                "trailingPE":                 self._num(info.get("trailingPE")),
                "priceToBook":                self._num(info.get("priceToBook")),
                "dividendYield":              self._num(info.get("dividendYield")),
                "revenueGrowth":              self._num(info.get("revenueGrowth")),
                "grossMargins":               self._num(info.get("grossMargins")),
                "regularMarketVolume":        self._num(info.get("regularMarketVolume") or info.get("volume")),
                "sector":                     info.get("sector", ""),
                "industry":                   info.get("industry", ""),
                "currency":                   currency,
                "market":                     market,
            }
        except Exception as e:
            print(f"  [ERROR] _fetch_quote {code} ({market}): {e}")
            return None

    # ── 銘柄ユニバース ────────────────────────────────
    _UNIVERSE = {
        "jp": [
            # 大型優良・配当
            "7203","7267","8306","8411","8316","9432","9433","8058","8002","8001",
            "5401","9101","5108","7011","4502","4568","8766","2914","9022","8591",
            "8309","8308","3382","4901","2801","7832","8804","4183","9020","9202",
            # テック・精密
            "6758","9984","7741","8035","6861","6098","6367","6724","6762","6752",
            "6971","4661","7733","6902","4543","6504","9613","6501","6301","7912",
            # 中堅優良
            "4523","4519","7751","6503","7201","4704","4755","7735",
            # 高成長・10倍候補
            "4385","6532","3994","4478","3697","4194","3923","4488","4849","7048",
        ],
        "us": [
            # メガテック
            "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","ORCL","AMD",
            # 金融
            "JPM","BAC","GS","MS","V","MA","BRK-B","C","WFC","AXP",
            # ヘルスケア・生活必需品
            "JNJ","PG","KO","MCD","WMT","ABBV","MRK","PFE","LLY","UNH",
            # エネルギー・素材
            "XOM","CVX","COP","SLB","FCX",
            # 通信・配当
            "T","VZ","INTC","IBM","MO",
            # その他優良
            "HD","LOW","CAT","BA","GE","QCOM","TXN","CSCO","ADBE","CRM",
            # 高成長・10倍候補
            "PLTR","CRWD","NET","DDOG","SNOW","S","ZS","MDB","GTLB","BILL",
        ],
    }

    # ── カテゴリ専用ユニバース（業界が明確に絞れるもの） ──
    # ここに定義のあるカテゴリはこちらが優先される。なければ _UNIVERSE 全体を使う。
    _CATEGORY_UNIVERSE = {
        "ai": {
            "jp": [
                "8035",  # 東京エレクトロン（半導体製造装置）
                "6857",  # アドバンテスト（半導体テスタ）
                "6146",  # ディスコ（半導体製造装置）
                "6920",  # レーザーテック（半導体検査）
                "3436",  # SUMCO（シリコンウェハー）
                "4063",  # 信越化学（半導体材料）
                "7735",  # SCREEN（半導体製造装置）
                "6963",  # ローム（半導体）
                "7741",  # HOYA（フォトマスク）
                "6967",  # 新光電気工業（半導体パッケージ）
                "6273",  # SMC（半導体製造装置部品）
                "6324",  # ハーモニック・ドライブ
                "6526",  # ソシオネクスト（カスタムLSI）
                "8053",  # 住友商事（AI関連投資）
                "9984",  # ソフトバンクG（Arm保有）
            ],
            "us": [
                "NVDA",  # 本命
                "AVGO",  # AIネットワーク半導体
                "AMD",   # GPU/CPU
                "TSM",   # 台湾セミコン（ファウンドリー最大手）
                "ASML",  # EUV露光装置
                "MU",    # メモリー
                "INTC",  # CPU/ファウンドリー
                "AMAT",  # 半導体製造装置
                "KLAC",  # 半導体検査
                "LRCX",  # エッチング装置
                "ARM",   # IPコア
                "MRVL",  # データセンター半導体
                "SMCI",  # AIサーバー
                "TXN",   # アナログ半導体
                "QCOM",  # モバイル/IoT
                "ON",    # オンセミ（パワー半導体）
                "MCHP",  # マイクロチップ
            ],
        },
        "tenx": {
            "jp": ["4385","6532","3994","4478","3697","4194","3923","4488","4849","7048"],
            "us": ["PLTR","CRWD","NET","DDOG","SNOW","S","ZS","MDB","GTLB","BILL"],
        },
        "tech": {
            "jp": [
                "6758","9984","7741","8035","6861","6098","6367","6724","6762","6752",
                "6971","4661","7733","6902","4543","6504","9613","6501","6301","7912",
                "6857","6146","6920","3436","6963",
            ],
            "us": [
                "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","ORCL","AMD",
                "CRM","ADBE","NOW","QCOM","TXN","CSCO","INTU","NFLX","ASML","TSM",
            ],
        },
    }

    def _score_stock(self, info, category="other"):
        """0〜100点でスコアリング（calcSignal のPython移植）"""
        per   = info.get("trailingPE")
        pbr   = info.get("priceToBook")
        div   = info.get("dividendYield")
        price = info.get("regularMarketPrice") or 0
        high52 = info.get("fiftyTwoWeekHigh") or 0
        low52  = info.get("fiftyTwoWeekLow")  or 0
        mkt    = info.get("market", "jp")
        is_growth = category in ("tech", "ai") or mkt == "us"

        scores = {}

        if per and 0 < per < 500:
            t = [15,22,30,45,65] if is_growth else [10,15,20,28,40]
            scores["per"] = 25 if per<t[0] else 20 if per<t[1] else 13 if per<t[2] else 7 if per<t[3] else 2 if per<t[4] else 0

        if pbr and pbr > 0:
            scores["pbr"] = 25 if pbr<1 else 20 if pbr<1.5 else 13 if pbr<2 else 7 if pbr<3 else 2 if pbr<5 else 0

        div_pct = (div * 100) if div else -1
        scores["div"] = (25 if div_pct>=4 else 20 if div_pct>=3 else 13 if div_pct>=2 else 7 if div_pct>=1 else 2) if div_pct >= 0 else 0

        if price and high52 and low52 and high52 > low52:
            pos = (price - low52) / (high52 - low52) * 100
            scores["pos"] = 25 if pos<=15 else 20 if pos<=30 else 13 if pos<=45 else 7 if pos<=60 else 2 if pos<=78 else 0

        weights = {
            "undervalue": {"per":2.0,"pbr":2.0,"div":1.0,"pos":1.0},
            "dividend":   {"per":1.0,"pbr":1.0,"div":2.5,"pos":1.5},
            "tech":       {"per":0.5,"pbr":0.5,"div":0.5,"pos":2.5},
            "ai":         {"per":0.3,"pbr":0.3,"div":0.2,"pos":3.0},
        }.get(category, {"per":1.0,"pbr":1.0,"div":1.0,"pos":1.0})

        if not scores:
            return None
        tw = sum(weights.get(k,1) for k in scores)
        ws = sum(scores[k]*weights.get(k,1) for k in scores)
        return round((ws / tw) * 4) if tw > 0 else None

    def _score_tenx(self, info):
        """10倍候補スコアリング（売上成長40%・時価総額35%・粗利率25%）"""
        revG = info.get("revenueGrowth")
        cap  = info.get("marketCap")
        gm   = info.get("grossMargins")
        is_us = info.get("market", "jp") == "us"

        scores = {}

        if revG is not None:
            scores["revG"] = (25 if revG >= 0.5 else 20 if revG >= 0.3 else
                              13 if revG >= 0.15 else 7 if revG >= 0.05 else
                              2  if revG >= 0    else 0)

        if cap is not None:
            # 小型ほど成長余地あり → 小さいほど高スコア
            t = [5e8, 2e9, 1e10, 5e10] if is_us else [5e10, 2e11, 1e12, 5e12]
            scores["cap"] = (25 if cap < t[0] else 20 if cap < t[1] else
                             13 if cap < t[2] else 7  if cap < t[3] else 2)

        if gm is not None:
            scores["gm"] = (25 if gm >= 0.7 else 20 if gm >= 0.5 else
                            13 if gm >= 0.3 else 7  if gm >= 0.15 else
                            2  if gm >= 0   else 0)

        weights = {"revG": 1.6, "cap": 1.4, "gm": 1.0}
        if not scores:
            return None
        tw = sum(weights.get(k, 1) for k in scores)
        ws = sum(scores[k] * weights.get(k, 1) for k in scores)
        return round((ws / tw) * 4) if tw > 0 else None

    def _screen_stocks(self, market="jp", category="other", exclude=None):
        """ユニバースからスクリーニングして上位銘柄を返す"""
        import concurrent.futures
        exclude = set(exclude or [])
        # カテゴリ専用ユニバースがあれば優先（AI半導体・10倍候補・テックなど）
        cat_uni = self._CATEGORY_UNIVERSE.get(category, {}).get(market)
        source   = cat_uni if cat_uni else self._UNIVERSE.get(market, [])
        universe = [c for c in source if c not in exclude]

        results = []

        def fetch_one(code):
            info = self._fetch_quote(code, market)
            return code, info

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for code, info in ex.map(fetch_one, universe):
                if not info:
                    continue
                if category == "tenx":
                    score = self._score_tenx(info)
                else:
                    score = self._score_stock(info, category)
                if score is None:
                    continue
                results.append({
                    "code":          code,
                    "name":          info.get("shortName") or code,
                    "price":         info.get("regularMarketPrice"),
                    "chgpct":        info.get("regularMarketChangePercent"),
                    "per":           info.get("trailingPE"),
                    "pbr":           info.get("priceToBook"),
                    "div":           info.get("dividendYield"),
                    "high52":        info.get("fiftyTwoWeekHigh"),
                    "low52":         info.get("fiftyTwoWeekLow"),
                    "cap":           info.get("marketCap"),
                    "revenueGrowth": info.get("revenueGrowth"),
                    "grossMargins":  info.get("grossMargins"),
                    "market":        market,
                    "score":         score,
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:12]

    # ── 関連銘柄取得 ──────────────────────────────────
    def _fetch_related(self, code, market="jp"):
        ticker_str = self._ticker(code, market)
        result = []

        # 1) Yahoo Finance の recommendationsbysymbol API
        try:
            url  = f"https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{ticker_str}"
            resp = req_lib.get(url, headers=HEADERS, timeout=10)
            data = resp.json()
            symbols = data["finance"]["result"][0]["recommendedSymbols"]
            for s in symbols:
                sym = s.get("symbol", "")
                # 同じ市場の銘柄だけ返す
                if market == "jp" and sym.endswith(".T"):
                    rel_code = sym[:-2]
                elif market == "us" and not sym.endswith(".T") and "." not in sym:
                    rel_code = sym
                else:
                    continue
                info = self._fetch_quote(rel_code, market)
                result.append({
                    "code":   rel_code,
                    "name":   info["shortName"] if info else rel_code,
                    "price":  info["regularMarketPrice"] if info else None,
                    "chgpct": info["regularMarketChangePercent"] if info else None,
                    "per":    info["trailingPE"] if info else None,
                    "pbr":    info["priceToBook"] if info else None,
                    "div":    info["dividendYield"] if info else None,
                    "market": market,
                    "score":  round(s.get("score", 0), 3),
                })
        except Exception as e:
            print(f"  [WARN] recommendationsbysymbol failed for {ticker_str}: {e}")

        # 2) 0件の場合はメッセージのみ返す
        if not result:
            result = [{"code": None, "name": None,
                       "message": "自動提案が見つかりませんでした。手動でコードを入力してください。"}]

        return result

    # ── レスポンス送信ヘルパー ─────────────────────────
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filename, content_type):
        filepath = os.path.join(HERE, filename)
        try:
            with open(filepath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json({"error": f"{filename} not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} -> {fmt % args}")


if __name__ == "__main__":
    print("=" * 48)
    print("  Stock Watchlist Server")
    print(f"  Open http://localhost:{PORT} in your browser")
    print("  Press Ctrl+C to stop")
    print("=" * 48)

    import threading, webbrowser, signal, sys
    def _open():
        import time; time.sleep(1.0)
        webbrowser.open(f"http://localhost:{PORT}")
    threading.Thread(target=_open, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), StockHandler)
    server.daemon_threads = True       # メインスレッド終了時に処理中スレッドも終了
    server.allow_reuse_address = True  # 再起動時に "Address in use" を回避

    # Ctrl+C / Ctrl+Break を即座に受け取って shutdown する
    def _stop(signum, frame):
        print("\nStopping server...")
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Server stopped.")
        sys.exit(0)
