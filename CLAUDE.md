# 株式ウォッチリスト アプリ 開発指針

## プロジェクト概要

超長期・買いのみの株式投資を支援するウォッチリストアプリ。
**売りは行わない**前提で、「いつ買うか」のタイミングアドバイスに特化する。

---

## 投資方針（3カテゴリ）

| カテゴリ | 説明 | 判断のポイント |
|---|---|---|
| 隠れ優良株（undervalue） | 実力があるのに低評価で、将来株価が爆上げする日本株 | PER・PBR の割安感、52週安値圏 |
| 先進技術（tech / ai） | 先行投資として有望な技術開発をしている会社 | 成長性・将来性重視、割高でも許容 |
| 安定配当（dividend） | 配当金を安定的に配当している会社 | 配当利回り3%以上、減配リスクの低さ |

---

## アーキテクチャ

```
watchlist.html   ← ブラウザで開く単一ファイルのUI
server.py        ← ローカルPythonサーバー（port 5000）
watchlist.json   ← ウォッチリストの永続化データ
start_server.bat ← Windows用起動スクリプト
```

### 起動方法
```
start_server.bat を実行 → ブラウザが http://localhost:5000 で自動オープン
```

### APIエンドポイント（server.py）

| メソッド | パス | 機能 |
|---|---|---|
| GET | `/` | watchlist.html を配信 |
| GET | `/api/quote?code=7203&market=jp` | 株価・財務データ取得（yfinance） |
| GET | `/api/history?code=7203&market=jp&period=1d` | ローソク足データ取得 |
| GET | `/api/related?code=7203&market=jp` | 関連銘柄提案取得 |
| GET | `/api/watchlist` | watchlist.json 読み込み |
| POST | `/api/watchlist` | watchlist.json 保存 |

### ティッカー変換ルール
- 日本株（market=jp）: コード + `.T` 例: `7203.T`
- 米国株（market=us）: コードそのまま 例: `NVDA`

---

## フロントエンド設計（watchlist.html）

### 主要な状態変数

```javascript
let watchlist  = [];      // 登録銘柄リスト（watchlist.jsonと同期）
let stockData  = {};      // { code: yahooQuoteData } キャッシュ
const expandedSet = new Set();  // 展開中の銘柄コード
const chartStates = {};         // { parentCode: { code, market, period } }
const chartInstances = {};      // LightweightCharts インスタンス
```

### データモデル（watchlist の各要素）

```json
{
  "code": "7203",
  "market": "jp",
  "category": "undervalue",
  "target": 2000,
  "memo": "メモ",
  "related": [
    { "code": "7267", "market": "jp" },
    { "code": "TSLA", "market": "us" }
  ]
}
```

### 遅延読み込みの原則
- **起動時**: メイン銘柄のみ株価取得。関連銘柄は取得しない。
- **行を展開したとき**: 未取得の関連銘柄データを取得してからパネル描画。
- **チャート**: `IntersectionObserver` でチャートエリアが画面内に入ったときだけ `/api/history` を呼ぶ。

### チャート（Lightweight Charts v3.8.0）

```javascript
// イントラデイ（1d/5d）: Unix タイムスタンプ（秒）
// 日次以上（1mo〜5y）: "YYYY-MM-DD" 文字列
// サーバーが "intraday": true/false フラグを付与
```

期間マッピング（server.py）:
```
1d  → 5分足   5d  → 30分足   1mo〜1y → 日足   5y → 週足
```

---

## 買いタイミング判定（calcSignal）

### 現在の実装（要改善）
52週レンジ内の現在値位置のみで判定：
- 0〜20%: 🟢 買いゾーン
- 20〜45%: 🟡 様子見
- 45〜70%: 🟠 高め
- 70〜100%: 🔴 高値圏

### 改善方針（未実装）
複数指標のスコアリング方式に変更する。カテゴリ別に重みを変える。

| 指標 | 隠れ優良株 | 先進技術 | 安定配当 |
|---|---|---|---|
| 52週位置（低いほど良） | ◎ 重視 | △ 参考程度 | ○ 重視 |
| PER（低いほど良） | ◎ 重視 | △ 高くても許容 | ○ 重視 |
| PBR（1倍以下で加点） | ◎ 重視 | △ 参考程度 | ○ 参考程度 |
| 配当利回り（高いほど良） | △ 参考程度 | △ 参考程度 | ◎ 重視 |
| 前日比（急落で加点） | ○ 加点 | ○ 加点 | ○ 加点 |

---

## 開発ルール

### やってはいけないこと
- `sell` / 売り に関する機能を追加しない（超長期買いのみの方針）
- YahooFinanceに直接ブラウザからアクセスする実装（CORSエラーになる）
- `watchlist.json` を直接ブラウザから書き込む実装

### データ永続化の二重化
- **localStorage**: キャッシュ（サーバーが落ちているときのフォールバック）
- **watchlist.json**: 正とするデータソース（サーバー経由で読み書き）

### 後方互換性
`normalizeRelated()` を必ず通して関連銘柄を参照する。
旧フォーマット（文字列配列）→ 新フォーマット（{code, market}配列）への自動変換が入っている。

---

## 将来の予定機能

- [ ] 買いタイミング判定のスコアリング方式改善（カテゴリ別重み付け）
- [ ] 自動買い機能（証券会社API連携）
- [ ] 株価アラート通知
- [ ] バックテスト（過去の買いシグナルの精度検証）

---

## 依存ライブラリ

```
Python: yfinance, http.server（標準ライブラリ）
JS: Lightweight Charts v3.8.0（unpkg CDN）
```

インストール:
```
pip install yfinance
```
