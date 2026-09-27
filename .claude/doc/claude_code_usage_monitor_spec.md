# Claude Code Usage Monitor 仕様書

## 1. 目的

Claude Codeを日常的に利用する際に、

- 現在の利用状況
- 5時間単位の利用状況
- 1日の利用状況
- 1週間の利用状況
- 利用制限のリセット時刻
- 使用量が制限に近づいた際の警告

を一元的に確認できるUsage Monitorを構築する。

主目的は「Claude Codeを使いすぎて突然利用制限に到達する」ことを防ぎ、利用可能量を意識しながら開発を継続できるようにすることである。

---

# 2. 重要な前提

## 2.1 Context使用量とSubscription Usageは別物

以下を混同しない。

### Context Usage

現在のClaude Codeセッションにおけるコンテキストウィンドウの使用量。

```text
Context
████████░░ 78%
156k / 200k tokens
```

これは現在のセッションのcontext使用量であり、サブスクリプションの5時間・日次・週次利用制限とは別の指標。

### Subscription Usage

Claude CodeのPro / Max等における利用制限。

```text
5-hour window
███████░░░ 70%

Weekly
█████░░░░░ 50%
```

こちらを本システムの主要監視対象とする。

---

# 3. 監視対象

## 3.1 Current Session

- 使用モデル
- Context使用率
- Input tokens
- Output tokens
- Cache usage
- Session ID
- Project / Working Directory

Claude Codeのstatusline等から取得可能な範囲で実装する。

## 3.2 5-hour Usage Window

最優先で取得する。

```text
5h Usage
Used:       XX%
Remaining:  XX%
Reset:      HH:MM
```

可能であればプログレスバー表示を行う。

## 3.3 Daily Usage

```text
Today
Used:       XX%
Remaining:  XX%
Reset:      YYYY-MM-DD HH:MM
```

Claude Code / Anthropicが日次使用量を直接提供していない場合は、独自集計とする。

## 3.4 Weekly Usage

```text
This Week
Used:       XX%
Remaining:  XX%
Reset:      YYYY-MM-DD HH:MM
```

同様に、公式API等から直接取得できない場合は独自集計とする。

---

# 4. データ取得方式

実装前に以下の優先順位で調査する。

## Priority 1: Official API / CLI

Anthropic / Claude Codeが公式に提供している情報を利用する。

調査対象:

- Claude Code CLI
- `/usage` 等のusage関連機能
- statusline
- Claude Code設定
- Claude API usage情報
- rate limit情報

公式に取得可能な値については最優先で利用する。

## Priority 2: Claude Code内部データ

公式APIで必要な情報が取得できない場合、Claude Codeがローカルに保存しているsession / usage関連情報を調査する。

ただし、

- 内部仕様に依存する
- Claude Codeアップデートで変更される可能性がある
- サブスクリプション利用制限そのものを表しているとは限らない

ため、内部データを使用する場合はAdapter層を設ける。

```text
UsageMonitor
      │
      ▼
UsageProvider
      │
 ┌────┴──────────────┐
 │                   │
OfficialProvider   LocalProvider
 │                   │
Claude API        Local data
CLI               Session data
```

## Priority 3: Web / Account側のUsage情報

ClaudeのWebアカウント側で利用量が確認できる場合、その情報を取得できる方法を調査する。

ただし、

- 非公開API
- ブラウザ内部API
- Cookie / Session情報

などに依存する場合は、正式なAPIではないことを明示する。

認証情報を平文保存しない。

---

# 5. 「正確な残量」と「推定値」を分離する

各Usage値にConfidenceを持たせる。

```text
Official
Estimated
Local Aggregation
Unavailable
```

例えば、

```text
5h Usage
████████░░ 80%
Source: Official
```

なら正確な利用制限情報。

一方、

```text
Weekly Usage
██████░░░░ 60%
Source: Estimated
```

なら「推定値」であることを表示する。

---

# 6. ローカルUsage Log

取得したusage情報をSQLite等に保存する。

推奨:

```text
usage.db
```

テーブル:

```text
usage_snapshots

id
timestamp
provider
model
session_id
project
context_used
context_limit
usage_5h
usage_daily
usage_weekly
reset_5h
reset_daily
reset_weekly
confidence
raw_data
```

---

# 7. Project単位の集計

複数プロジェクトでClaude Codeを使用するため、project単位でもUsageを記録する。

例:

```text
Projects

Progress Navi       42%
3D Modeling         31%
Dog Camera          17%
Other               10%
```

ただし、Subscription Usageそのものがproject単位で提供されていない場合は、「推定された消費量」として扱う。

Subscription quotaをプロジェクト別に勝手に配分して「公式値」のように表示してはいけない。

---

# 8. CLI

基本コマンド:

```bash
claude-usage
```

表示:

```text
Claude Code Usage Monitor
────────────────────────────────

Model
  Opus 5

5-hour window
  ███████░░░ 72%
  Remaining: 28%
  Reset: 14:32

Today
  ██████░░░░ 61%
  Remaining: 39%

This week
  █████░░░░░ 48%
  Remaining: 52%

────────────────────────────────
Updated: 12:41:32
```

---

# 9. 詳細表示

```bash
claude-usage status
```

```text
Claude Code Usage
────────────────────────────────

5h Window
  Used       : 72%
  Remaining  : 28%
  Reset      : 14:32
  Source     : Official

Today
  Used       : 61%
  Remaining  : 39%
  Source     : Estimated

This Week
  Used       : 48%
  Remaining  : 52%
  Source     : Estimated

Current Session
  Model      : Opus 5
  Context    : 64%
  Input      : 124,532
  Output     : 31,204

Last update
  12:41:32
```

---

# 10. JSON Output

外部ツールとの連携を考慮し、JSON出力を必須とする。

```bash
claude-usage --json
```

例:

```json
{
  "timestamp": "2026-09-27T12:41:32+09:00",
  "subscription": {
    "five_hour": {
      "used_percent": 72,
      "remaining_percent": 28,
      "reset_at": "2026-09-27T14:32:00+09:00",
      "confidence": "official"
    },
    "daily": {
      "used_percent": 61,
      "remaining_percent": 39,
      "reset_at": null,
      "confidence": "estimated"
    },
    "weekly": {
      "used_percent": 48,
      "remaining_percent": 52,
      "reset_at": null,
      "confidence": "estimated"
    }
  }
}
```

---

# 11. Alert

閾値を設定可能にする。

デフォルト:

```text
70%  WARNING
85%  HIGH
95%  CRITICAL
```

例:

```text
WARNING

Claude Code 5h usage has reached 70%.

Remaining: 30%
Reset: 14:32
```

---

# 12. Notification

将来的に以下へ通知可能な構造にする。

- Windows Notification
- Discord
- Slack
- Terminal
- Web UI

Notification Providerを抽象化する。

```text
AlertManager
     │
     ├── WindowsNotification
     ├── DiscordNotifier
     └── SlackNotifier
```

---

# 13. Statusline連携

Claude Codeのstatuslineに以下を表示できるようにする。

```text
Opus 5 | Context 64% | 5h 72% | Day 61% | Week 48%
```

ただし、statuslineから取得できるContext情報とSubscription Usageを明確に分離する。

---

# 14. Windows対応

Windows 11を想定する。

優先:

```text
PowerShell
```

必要に応じて:

```text
WSL
```

でも動作可能とする。

環境依存部分をAdapter化する。

---

# 15. 自動更新

Usage情報は一定間隔で更新する。

デフォルト:

```text
30 seconds
```

設定:

```yaml
refresh_interval: 30
```

公式APIにアクセスする場合はAPIへの過剰なアクセスを避ける。

---

# 16. データ保存期間

Usage logは長期保存可能とする。

デフォルト:

```text
90 days
```

設定変更可能。

```yaml
retention_days: 90
```

---

# 17. セキュリティ

以下を絶対にUsage DBへ保存しない。

- API Key
- OAuth Token
- Session Cookie
- Password
- Authorization Header

認証情報を利用する必要がある場合は、OSのCredential Manager等を利用する。

---

# 18. アーキテクチャ

```text
┌──────────────────────────────┐
│       Claude Usage Monitor   │
└──────────────┬───────────────┘
               │
               ▼
        Usage Aggregator
               │
       ┌───────┴────────┐
       ▼                ▼
Official Provider   Local Provider
       │                │
       ▼                ▼
Claude / API       Local data
       │                │
       └───────┬────────┘
               ▼
          Normalizer
               │
               ▼
          SQLite DB
               │
       ┌───────┼──────────┐
       ▼       ▼          ▼
      CLI   Statusline   Alert
                         │
                   ┌─────┼─────┐
                   ▼     ▼     ▼
                 Win  Slack  Discord
```

---

# 19. 実装フェーズ

## Phase 0: 調査

Claude Code自身に以下を調査させる。

1. 現在のClaude Codeバージョン
2. `/usage`等のusage関連コマンド
3. CLIから取得できるusage情報
4. statuslineから取得できる情報
5. ローカルに保存されるsession / usageデータ
6. Claude Web / Account側で取得できるusage情報
7. 5時間制限の残量を取得する方法
8. Weekly limitの残量を取得する方法
9. 日次limitの残量を取得する方法
10. 公式APIと非公式APIの区別

---

## Phase 1: Read-only Monitor

まずデータ取得だけ実装する。

```bash
claude-usage
```

で現在の状態を確認できるようにする。

この段階では通知やDashboardを作らない。

---

## Phase 2: Persistent Logging

SQLiteにUsage Snapshotを保存する。

```bash
claude-usage daemon
```

でバックグラウンド監視。

---

## Phase 3: Alert

閾値監視を追加。

```text
70%
85%
95%
```

---

## Phase 4: Statusline

Claude Codeのstatuslineへ統合。

---

## Phase 5: Dashboard

必要になった場合のみWeb UIを追加。

候補:

```text
localhost:xxxx
```

表示:

- 5h Usage
- Daily Usage
- Weekly Usage
- Usage history
- Reset history
- Project usage
- Model usage

---

# 20. 最重要設計方針

このツールでは、

> 「取得できない値を推測して、それを実際のQuota残量として表示しない」

ことを最重要ルールとする。

例えばWeekly Usageを正確に取得できない場合、

```text
This Week
Unavailable
```

と表示する。

独自ログから推定できる場合のみ、

```text
This Week
Estimated: 48%
```

と表示する。

---

# 21. Claude Codeへの初期調査指示

まず実装を開始せず、以下の調査を実施する。

```text
Claude Code Usage Monitorの実装を開始する前に、
Claude CodeおよびAnthropicが提供しているUsage / Rate Limit情報を徹底的に調査してください。

目的は、Claude CodeのSubscription Usageについて、

1. 現在の5時間Usage
2. 5時間Usageの残量
3. 5時間WindowのReset時刻
4. Daily Usage
5. Daily Usageの残量
6. Weekly Usage
7. Weekly Usageの残量
8. Weekly Reset時刻

をプログラムから取得する方法を特定することです。

以下を優先順位順に調査してください。

A. Anthropic公式API
B. Claude Code公式CLI
C. Claude Codeの公式ドキュメント
D. Claude Codeのローカルデータ
E. Claude Web / Account側のAPI
F. その他の方法

各方法について、

- 取得できる情報
- 取得方法
- API / CLI / ローカルデータの別
- Official / Unofficial
- 認証方法
- Claude Codeのバージョン依存性
- 将来的に壊れる可能性
- 実際のSubscription Quotaを表しているか
- 推定値なのか正確な値なのか

を整理してください。

特に重要なのは、
「Context Windowの使用率」
「API Token Usage」
「Claude Code Subscription Usage」
「Rate Limit」
を明確に区別することです。

調査結果を踏まえて、
Claude Code Usage Monitorを実装する場合の最も堅牢なデータ取得方式を提案してください。

まだコードは実装しないでください。
まず調査結果と推奨アーキテクチャを提示してください。
```

---

# 22. 完成イメージ

最終的にはClaude Codeを使っている最中に、

```text
────────────────────────────────────────
 Opus 5

 Context     ██████░░░░  62%

 5h Usage    ████████░░  78%
             Reset 14:32

 Today       ██████░░░░  61%

 This Week   █████░░░░░  49%

 ✓ Usage normal
────────────────────────────────────────
```

という状態を常に確認できることを目標とする。
