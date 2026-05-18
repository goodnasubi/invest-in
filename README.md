# 📈 株式ウォッチリスト

超長期・買いのみの株式投資を支援するローカルウォッチリストアプリ。
日本株・米国株を一元管理し、PER/PBR/配当利回り/52週位置などからカテゴリ別の買いタイミングをスコアリングします。
Claude API 連携で、各銘柄のスコア根拠を自然言語で解説する機能も搭載。

![Architecture](https://img.shields.io/badge/Python-3.8+-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Status](https://img.shields.io/badge/Status-Personal_Project-orange)

---

## ✨ 主な機能

- **5カテゴリでの投資管理**
  - 💎 割安優良株 / 🚀 先進技術 / 💰 安定配当 / 🤖 AI半導体 / 💥 10倍候補
- **カテゴリ別スコアリング（0〜100点）**
  - PER・PBR・配当利回り・52週位置を、カテゴリごとに重み付けして総合判定
  - 10倍候補は売上成長率・時価総額・粗利率で評価
- **目標価格アラート** — 設定価格に近づくと自動通知
- **W買いシグナル** — 買いスコア + 目標価格接近のダブル成立で強調表示
- **ローソク足チャート** — 1日〜5年（Lightweight Charts）
- **関連銘柄管理** — Yahoo Financeの自動提案 + 手動追加
- **自動スクリーニング** — 約100銘柄のユニバースから条件に合う銘柄を抽出
- **自動更新** — 5/15/30分間隔で価格再取得、シグナル変化を通知
- **💬 AI解説（Claude API）** — スコアの根拠を3行で説明

---

## 🏗 構成

```
watchlist.html         ← ブラウザで開く単一ファイルのUI
server.py              ← ローカルPythonサーバー（port 5000）
watchlist.json         ← ウォッチリストの永続化（.gitignore対象）
watchlist.example.json ← サンプルデータ
start_server.bat       ← Windows用起動スクリプト
pyproject.toml         ← プロジェクト定義 + 依存
uv.lock                ← 依存ロックファイル
```

---

## 🚀 セットアップ

### 前提: [uv](https://docs.astral.sh/uv/) をインストール
```powershell
# Windows
winget install --id=astral-sh.uv

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. 依存パッケージのインストール
```bash
uv sync
```
これで `.venv` に必要なパッケージが自動でインストールされます。

### 2. （任意）Claude APIキーを環境変数に設定
AI解説機能を使う場合のみ必要です。
```powershell
# Windows (PowerShell)
setx ANTHROPIC_API_KEY "sk-ant-api03-xxxxx..."
```
APIキーは https://console.anthropic.com から取得できます（最低$5から購入可、1回の解説あたり約0.2円）。

### 3. サンプルデータをコピー（初回のみ）
```bash
cp watchlist.example.json watchlist.json
```

### 4. サーバー起動
```bash
# Windows
start_server.bat

# その他
uv run python server.py
```
ブラウザで http://localhost:5000 が自動で開きます。

---

## 📊 スコアリング方式

### 通常カテゴリ（割安優良 / 配当 / 先進技術 / AI半導体）

| 指標 | 配点 | 割安優良 | 安定配当 | 先進技術 | AI半導体 |
|---|---|---|---|---|---|
| PER（低いほど良） | 25点 | ×2.0 | ×1.0 | ×0.5 | ×0.3 |
| PBR（1倍以下で加点） | 25点 | ×2.0 | ×1.0 | ×0.5 | ×0.3 |
| 配当利回り（高いほど良） | 25点 | ×1.0 | ×2.5 | ×0.5 | ×0.2 |
| 52週位置（低いほど良） | 25点 | ×1.0 | ×1.5 | ×2.5 | ×3.0 |

### 10倍候補

| 指標 | 配点 | 重み |
|---|---|---|
| 売上成長率（>50%で満点） | 25点 | ×1.6 |
| 時価総額（小型ほど高評価） | 25点 | ×1.4 |
| 粗利率（>70%で満点） | 25点 | ×1.0 |

### 判定ラベル

| スコア | 通常 | 10倍候補 |
|---|---|---|
| 70点以上 | 🟢 強い買い | 💥 期待大 |
| 55〜69 | 🟡 買い検討 | 🌱 有望 |
| 38〜54 | 🟠 様子見 | 🔍 要調査 |
| 37以下 | 🔴 割高 | ⚪ 低確率 |

---

## 🔌 APIエンドポイント

| メソッド | パス | 機能 |
|---|---|---|
| GET | `/` | UIを配信 |
| GET | `/api/quote?code=7203&market=jp` | 株価・財務データ取得 |
| GET | `/api/history?code=7203&market=jp&period=1d` | ローソク足データ取得 |
| GET | `/api/related?code=7203&market=jp` | 関連銘柄提案 |
| GET | `/api/screen?market=jp&category=undervalue` | スクリーニング実行 |
| GET | `/api/watchlist` | ウォッチリスト読み込み |
| POST | `/api/watchlist` | ウォッチリスト保存 |
| POST | `/api/explain` | Claude AIによるスコア解説 |

---

## 🛠 技術スタック

- **バックエンド**: Python標準ライブラリ（`http.server`）+ [yfinance](https://github.com/ranaroussi/yfinance)
- **AI解説**: [Anthropic Claude API](https://www.anthropic.com/api)（claude-haiku-4-5）
- **フロントエンド**: 単一HTML + バニラJS
- **チャート**: [Lightweight Charts v3.8.0](https://www.tradingview.com/lightweight-charts/)

外部CDN・ビルドツール不要。Pythonとブラウザだけで動きます。

---

## ⚠️ 注意

- 本アプリは投資判断の**補助ツール**であり、投資助言ではありません。
- すべての投資判断はご自身の責任で行ってください。
- 株価データは Yahoo Finance 由来です。リアルタイム性は保証されません。
- **APIキー・watchlist.jsonは絶対にGitにコミットしないでください**（`.gitignore` で除外済み）。

---

## 📄 ライセンス

[MIT](LICENSE)
