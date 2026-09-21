# Slack × Claude Code ローカル開発ブリッジ仕様

## 1. 目的

スマートフォンからSlackをインターフェースとして、自宅Windows PC上で動作しているClaude Codeに開発タスクを依頼できる環境を構築する。

複数の開発プロジェクトを扱い、SlackのチャンネルによってClaude Codeの作業対象プロジェクトを自動的に切り替える。

最終的な利用イメージ：

```text
📱 スマートフォン
      ↓
    Slack
      ↓
┌──────────────────────────────┐
│ #progress-navi               │
│ #pet-camera                  │
│ #business                    │
└──────────────┬───────────────┘
               ↓
        Slack Bridge
               ↓
       Project Routing
               ↓
      ┌────────┴────────┐
      ↓                 ↓
working directory   Claude Code
      ↓
各プロジェクトのRepository
```

目的は、外出先からVS CodeやWindowsデスクトップを遠隔操作することなく、SlackだけでClaude Codeに開発作業を依頼できるようにすることである。

---

# 2. 基本方針

## 2.1 Claude Codeは自宅PC上で実行する

各プロジェクトのソースコードは基本的に自宅Windows PCに置く。

クラウドへ開発環境を移行することはMVPの目的としない。

例：

```text
C:\Users\yutob\work_env\
├── navigation-core\
├── pet-camera\
└── business\
```

Claude CodeもこのPC上で実行する。

---

## 2.2 Slackは「開発インターフェース」とする

SlackはClaude Codeそのものではなく、Claude Codeへタスクを渡すためのUIとして利用する。

例えば：

```text
#progress-navi

@ClaudeCode
FastAPIとStreamlitを分離する場合の構成について調査して。
現在のコードを確認した上で、
推奨構成と移行手順をまとめて。
今回はコード変更はしないで。
```

Slack Bridgeがメッセージを受信し、Progress Naviのworking directoryでClaude Codeを実行する。

---

# 3. 複数プロジェクト対応

## 3.1 基本設計

**Slackのチャンネルとプロジェクトを1対1で対応させる。**

例：

```text
#progress-navi
    ↓
C:\Users\yutob\work_env\navigation-core

#pet-camera
    ↓
C:\Users\yutob\work_env\pet-camera

#business
    ↓
C:\Users\yutob\work_env\business
```

Slack Bridgeは受信したメッセージのchannel_idから対象プロジェクトを特定する。

---

## 3.2 プロジェクト設定

プロジェクトとSlackチャンネルの対応関係は設定ファイルで管理する。

例：

```yaml
projects:

  progress-navi:
    channel_id: "CXXXXXXXX"
    name: "Progress Navi"
    working_directory: "C:/Users/yutob/work_env/navigation-core"

  pet-camera:
    channel_id: "CYYYYYYYY"
    name: "Pet Camera"
    working_directory: "C:/Users/yutob/work_env/pet-camera"

  business:
    channel_id: "CZZZZZZZ"
    name: "Business"
    working_directory: "C:/Users/yutob/work_env/business"
```

チャンネル名ではなく**Slackのchannel_idを内部的な識別子として使用する。**

これにより、チャンネル名変更による誤動作を防ぐ。

---

# 4. プロジェクトルーティング

処理フロー：

```text
Slack message
      ↓
channel_id取得
      ↓
Project Mapping
      ↓
対象プロジェクト決定
      ↓
working_directory決定
      ↓
Claude Code起動
```

例えば：

```text
Slack:
#progress-navi
    ↓
channel_id = CXXXXXXXX
    ↓
project = progress-navi
    ↓
cwd =
C:\Users\yutob\work_env\navigation-core
    ↓
Claude Code
```

別のチャンネルでは：

```text
Slack:
#pet-camera
    ↓
channel_id = CYYYYYYYY
    ↓
project = pet-camera
    ↓
cwd =
C:\Users\yutob\work_env\pet-camera
    ↓
Claude Code
```

---

# 5. MVPの対象

最初のMVPでは、以下を実装する。

### 必須

1. Slack Botを作成
2. 特定チャンネルのメッセージを受信
3. `@ClaudeCode` へのメンションを検知
4. メッセージ本文を取得
5. channel_idからプロジェクトを特定
6. プロジェクトのworking directoryを取得
7. 自宅PC上のClaude Code CLIへ渡す
8. Claude Codeの実行結果を取得
9. Slackのスレッドへ結果を返信

### MVPでは対象外

- 複数ユーザー対応
- 複数PC対応
- 複数Claude Codeプロセスの並列実行
- 自動マージ
- 自動デプロイ
- 本番環境操作
- 高度な権限管理
- Web UI

まずは、

**「Slackチャンネル → 対象プロジェクト → Claude Code → Slack」**

という一本の流れを完成させる。

---

# 6. Slack側

## 6.1 開発用チャンネル

プロジェクトごとにSlackチャンネルを作成する。

例：

```text
#progress-navi
#pet-camera
#business
```

将来的にプロジェクトが増えた場合も、設定ファイルへ追加することで対応する。

---

## 6.2 Slack App

Slack Appを作成する。

必要な機能：

- Bot User
- Events API
- Web API

最低限必要となるイベント：

```text
app_mention
```

Botへのメンションをトリガーとして処理する。

---

# 7. Slack → Claude Code

Slackで：

```text
@ClaudeCode
技術調査して。

FastAPIとStreamlitを分離した場合の構成を調査してください。
現在のコードを確認した上で、

・現状
・推奨構成
・メリット
・デメリット
・移行手順

をまとめてください。

コード変更は不要です。
```

と投稿する。

Slack Bridgeはメンション部分を除去し、Claude Codeへのプロンプトを生成する。

---

# 8. Claude Codeの実行

Claude Code CLIの非対話モードを使用する。

基本形：

```bash
claude -p "<prompt>"
```

必要に応じてJSON形式を利用する。

```bash
claude -p "<prompt>" --output-format json
```

Claude Codeは対象プロジェクトのworking directoryから起動する。

---

# 9. Claude Codeの作業ディレクトリ

Claude Code起動時に必ず対象プロジェクトのworking directoryを指定する。

例：

```text
C:\Users\yutob\work_env\navigation-core
```

実行イメージ：

```text
Slack
 ↓
#progress-navi
 ↓
Project Mapping
 ↓
navigation-core
 ↓
Claude Code
```

Claude Codeは対象リポジトリの

```text
CLAUDE.md
skills/
rules/
```

など既存の開発ルールを利用する。

---

# 10. タスク種別

MVPでは、まず以下の2種類を想定する。

## 10.1 調査タスク

例：

```text
FastAPIとStreamlitを分離する場合の現在の問題点を調査して。
```

期待する動作：

```text
Slack
 ↓
Claude Code
 ↓
コードベースを調査
 ↓
結果をまとめる
 ↓
Slackへ返信
```

コード変更は行わない。

---

## 10.2 実装タスク

例：

```text
ログイン画面のエラーメッセージを改善して。
テストも実行して。
```

期待する動作：

```text
Slack
 ↓
Claude Code
 ↓
コード調査
 ↓
実装
 ↓
テスト
 ↓
Slackへ結果
```

ただし、MVPでは実装権限を制限する。

---

# 11. Claude Codeセッション管理

SlackのスレッドとClaude Codeのセッションを将来的に紐付ける。

```text
#progress-navi
│
├── Thread A
│      ↓
│   Claude Code Session A
│
├── Thread B
│      ↓
│   Claude Code Session B
│
└── Thread C
       ↓
    Claude Code Session C
```

これにより、

```text
ユーザー:
@ClaudeCode FastAPI化について調査して

Claude:
調査しました。......

ユーザー:
@ClaudeCode
さっきの調査の続きをやって。
```

という会話を同じClaude Codeセッションに継続できるようにする。

ただし、セッション継続機能はMVP後の拡張として扱う。

MVPでは各メッセージを独立したClaude Code実行としてもよい。

---

# 12. 長時間タスク

Claude Codeの処理には時間がかかる可能性がある。

そのため、受信直後にSlackへ：

```text
🔍 調査を開始しました。
Project: Progress Navi
```

と返信する。

処理完了後：

```text
✅ 調査完了しました。

[結果]

...
```

と同じスレッドへ返信する。

---

# 13. Slackへの返信

Claude Codeの実行結果をSlack向けに整形して返信する。

例：

```text
✅ 調査完了しました。

## 現状

現在の構成ではStreamlitとAPIロジックが密結合しています。

## 推奨構成

Frontend
  ↓
FastAPI
  ↓
Service layer
  ↓
Database

## 主な理由

- ...
- ...
- ...

## 詳細

...
```

返信には対象プロジェクト名も表示する。

```text
Project: Progress Navi
```

これにより、複数チャンネルを横断して見た場合でも対象を確認できる。

---

# 14. エラー処理

Claude Codeが失敗した場合：

```text
❌ Claude Codeの実行に失敗しました。

Project:
Progress Navi

Error:
...

詳細ログ:
...
```

Slack Bridge自体が停止している場合も、原因をローカルログへ記録する。

---

# 15. 未登録チャンネル

Slack Bridgeが未知のchannel_idを受信した場合、Claude Codeを実行しない。

Slackへ：

```text
⚠️ このチャンネルにはClaude Codeのプロジェクトが設定されていません。

channel_id:
CXXXXXXXX

プロジェクト設定を確認してください。
```

と返信する。

**未知のチャンネルでClaude Codeを実行しないこと。**

---

# 16. セキュリティ

Slack Events APIを受信するエンドポイントは外部公開されるため、Slack Signing Secretを利用してリクエストを検証する。

環境変数：

```text
SLACK_BOT_TOKEN
SLACK_SIGNING_SECRET
CLAUDE_WORKING_DIRECTORY
```

ただし複数プロジェクトのworking directoryは設定ファイルで管理してよい。

秘密情報はGit管理しない。

`.env`はGitignoreする。

---

# 17. 外部公開

自宅Windows PC上でSlack Events APIを受け取るため、Slackから自宅PCへ到達できる仕組みが必要。

MVPでは以下を候補とする。

- Cloudflare Tunnel
- ngrok
- Tailscale Funnel
- その他の安全なHTTPSトンネル

最も簡単で安全な方式を選択する。

---

# 18. 技術スタック

MVPでは以下を第一候補とする。

```text
Python
FastAPI
Slack Bolt for Python
Claude Code CLI
```

構成：

```text
slack-bridge/
├── app/
│   ├── main.py
│   ├── slack_handler.py
│   ├── claude_runner.py
│   ├── project_router.py
│   └── config.py
├── config/
│   └── projects.yaml
├── tests/
├── .env
├── .gitignore
└── README.md
```

---

# 19. 処理フロー

```text
1. Slackにメッセージ投稿
        ↓
2. Slack Events API
        ↓
3. FastAPIがイベント受信
        ↓
4. Slack署名を検証
        ↓
5. @ClaudeCodeメンションを検出
        ↓
6. channel_idを取得
        ↓
7. Project Routerでプロジェクトを特定
        ↓
8. working directoryを決定
        ↓
9. Slackへ「処理開始」を返信
        ↓
10. 対象directoryでClaude Code CLIを起動
        ↓
11. Claude Codeが調査/変更/テスト
        ↓
12. 結果を取得
        ↓
13. 結果をSlack用に整形
        ↓
14. 元メッセージのthread_tsへ返信
```

---

# 20. 権限・安全性

Slackから送られた指示を、そのまま無制限にClaude Codeへ渡さない。

最低限、以下を検討する。

```text
調査
 → 許可

コード変更
 → MVPでは明示的に許可

git commit
 → MVPでは要検討

git push
 → 原則禁止

git merge
 → 禁止

本番環境操作
 → 禁止
```

特に、

```bash
--dangerously-skip-permissions
```

を安易に使用しない。

Claude Codeの権限管理機能を利用する。

---

# 21. GitHub連携

MVP完成後にGitHubとの連携を追加する。

想定フロー：

```text
Slack
 ↓
Claude Code
 ↓
git branch
 ↓
コード変更
 ↓
test
 ↓
commit
 ↓
push
 ↓
GitHub PR
 ↓
Slack通知
```

自動マージは当面行わない。

---

# 22. GitHub Actions連携

GitHub Actionsの結果をSlackへ通知する。

例：

```text
PR #42
 ↓
GitHub Actions
 ↓
test
 ↓
成功/失敗
 ↓
Slack
```

将来的にはClaude Codeがテスト失敗を検知し、原因調査を行うフローも検討する。

---

# 23. 将来拡張

## 23.1 プロジェクト追加

新しいプロジェクトを追加する場合：

```yaml
projects:

  new-project:
    channel_id: "CXXXXXXXX"
    name: "New Project"
    working_directory: "C:/Users/yutob/work_env/new-project"
```

を追加する。

コード変更なしでプロジェクトを追加できる設計を目指す。

---

## 23.2 セッション継続

Slack threadとClaude Code session_idを保存する。

```text
Slack thread
      ↓
session_id
      ↓
Claude Code session
```

これによりSlack上の会話をClaude Codeの継続セッションとして扱えるようにする。

---

## 23.3 プロジェクトごとのルール

将来的にプロジェクトごとに異なるClaude Codeルールを利用する。

```text
Progress Navi
 ├── CLAUDE.md
 └── skills/

Pet Camera
 ├── CLAUDE.md
 └── skills/
```

Slack Bridge側ではプロジェクトのworking directoryだけを指定し、各プロジェクト固有のルールは各リポジトリ側で管理する。

---

# 24. 最終的な目標

スマートフォンからSlackだけで複数プロジェクトの開発を進められる状態を目指す。

```text
                         📱
                       Slack
                         │
          ┌──────────────┼──────────────┐
          ↓              ↓              ↓
   #progress-navi   #pet-camera     #business
          │              │              │
          ↓              ↓              ↓
    navigation-core  pet-camera     business
          │              │              │
          └──────────────┼──────────────┘
                         ↓
                  Claude Code
                         ↓
              調査 / 実装 / テスト
                         ↓
                       Slack
                         ↓
                    📱 結果確認
```

## MVPの完成条件

以下が実際に動作すること。

### Test 1：Progress Navi

Slackの `#progress-navi` で：

```text
@ClaudeCode
現在のProgress Naviの構成を調査して、
主要なディレクトリと役割をまとめて。
コード変更はしないで。
```

→ `navigation-core` を対象にClaude Codeが実行される。

### Test 2：Pet Camera

Slackの `#pet-camera` で：

```text
@ClaudeCode
現在のシステム構成を調査してまとめて。
コード変更はしないで。
```

→ `pet-camera` を対象にClaude Codeが実行される。

### Test 3：未知のチャンネル

未登録チャンネルで：

```text
@ClaudeCode
調査して。
```

→ Claude Codeを実行せず、未登録チャンネルであることをSlackへ返す。

---

# 25. 開発方針

まずは最小構成で動かす。

優先順位：

```text
Phase 1
Slack受信
 ↓
channel_id判定
 ↓
project routing
 ↓
Claude Code実行
 ↓
Slack返信

Phase 2
調査タスクの品質改善
 ↓
長時間処理
 ↓
エラー処理

Phase 3
Claude Code session継続

Phase 4
Git / GitHub連携

Phase 5
GitHub Actions連携

Phase 6
より高度な自律開発フロー
```

**最初から高度なエージェントシステムを作らず、「Slackのチャンネルを切り替えるだけで、対象プロジェクトのClaude Codeが切り替わる」ことを最初のゴールとする。**
