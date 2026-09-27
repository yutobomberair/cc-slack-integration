# cc-slack-integration

スマートフォンの Slack から、自宅 PC で動く Claude Code に開発を任せるためのブリッジ。

外出先でコードを直したいとき、これまでは家の PC をリモート操作するしかなかった。
このブリッジは Slack を入口にして、**チャンネルを切り替えるだけで対象プロジェクトが
切り替わる**ようにする。VS Code もデスクトップも開かない。

```
                          📱 Slack
                             │
        ┌────────────────────┼────────────────────┐
        ↓                    ↓                    ↓
 #navigation-core       #pet-camera          #business-contest
        │                    │                    │
        └────────────────────┼────────────────────┘
                             ↓
                      Slack Bridge
                   （自宅 Windows PC）
                             ↓
                   channel_id → プロジェクト
                             ↓
                   対象ディレクトリで claude -p
                             ↓
                  調査 / 実装 / テスト / コミット
                             ↓
                       📱 スレッドへ返信
```

Slack の Events API は使わず **Socket Mode** で繋ぐ。自宅 PC に公開ポートも HTTPS
トンネルも要らず、外向きの WebSocket を1本張るだけで済む。

## 使い方

メンションするだけ。前置きは要らない。

```
@ClaudeCode
ログイン画面のエラーメッセージを改善して。テストも通して。
```

依頼文の先頭で、その場だけモードやモデルを変えられる。

| 書き方 | 意味 |
|---|---|
| （何も書かない） | プロジェクトの既定プロファイルで実行 |
| `調査:` `read:` | 読み取り専用に降格。ファイルは一切触らせない |
| `haiku:` `opus:` | モデルを指定（`allowed_models` にある名前だけ） |
| `調査 haiku:` | 併用可。順番は問わない |
| `共有: 1` | 出力ファイルを番号で受け取る |
| `cd: movie` | 作業階層を変える（後述） |
| `ls` | いまの階層のディレクトリを見る |

スレッドは Claude Code のセッションに紐づく。同じスレッドで続けて話せば文脈が残る。

```
@ClaudeCode さっきの調査の続きで、移行手順だけ詳しく出して
```

## 作業階層を選ぶ

Claude Code は**起動した場所から `.claude/` を探す**ので、プロジェクト内に開発ルールが
複数階層あると、どこで起動するかで読まれる CLAUDE.md と skills が変わる。

```
propose/
├── .claude/CLAUDE.md, skills/      ← propose で起動すると読まれる
└── movie/
    └── .claude/CLAUDE.md, skills/  ← propose/movie で起動すると読まれる
```

まず `ls` で何があるか見る。移動先の候補が分からないと `cd:` を打てない。

```
📂 プロジェクト直下 の中身  📖 この階層に開発ルールあり
Project: propose

📁 metting/
📁 movie/  📖 開発ルールあり

`.gitignore` `.mcp.json` `PLAN.md`

移動するには cd: <名前>。
```

`ls` はコロン無しでも動く。`ls: movie` のように引数を付けると、移動せずにその中を
覗ける。`venv` や `node_modules` は移動先にならないので隠す。

`cd:` でその階層を選ぶ。**スレッド単位で持続**するので、一度指定すれば以降の依頼は
そこで実行される。

```
@ClaudeCode cd: movie     → このスレッドは propose/movie で実行
@ClaudeCode cd:           → 現在の階層と、開発ルールがある階層の一覧
@ClaudeCode cd: .         → プロジェクト直下に戻す
```

どこで動いているかは毎回の返信に出る。

```
⏳ 処理を開始しました。
Project: propose / 📁 movie/ / ✏️ 実装モード
```

**プロジェクト配下に限る。** `..` や絶対パスは弾く。ここに Slack からの入力が
そのまま通ると、`config/projects.yaml` による範囲指定の歯止めが効かなくなる。

階層ごとに Claude Code のセッションが分かれる（セッションは作業ディレクトリ単位で
保存されるため）。行って戻ってくれば、それぞれの会話が続く。

## ファイルの受け渡し

生成物は自宅 PC の中にあるので、そのままではスマホから見えない。双方向の経路がある。

**受け取る** — 実行後に出力ファイルの一覧が届き、欲しいものを番号で取り出す。

```
📎 この実行で出力されたファイル
`1` `docs/report.md` （12.4 KB）
`2` `data/summary.csv` （3.1 KB)

→ @ClaudeCode 共有: 1
```

毎回全部添付しないのは、実装タスクだとソースが十数ファイル変わってスレッドが
埋まるため。番号のほか `共有: report` のようにファイル名の部分一致でも指定できる。

**Claude から送る** — `_share/` に置かれたファイルは一覧を挟まず自動で添付される。
Claude に Slack のトークンを触らせずに済ませるための出口で、送信の待ち行列として
扱う（送れたら `_share/sent/` へ退き、失敗したものは残って次の実行で再試行される）。

**渡す** — メンションにファイルを添付すると `_inbox/` へ降り、置き場所が Claude に
伝わる。スマホで撮った資料をそのまま投げて「これを元に整理して」と頼める。

`_share/` と `_inbox/` は `cd:` で階層を変えても**常にプロジェクト直下**に置く。
階層ごとに散ると `共有: 1` の番号と実体の対応が追えなくなるため。下の階層で動いて
いる Claude には `../_share/` のような相対パスで伝える。

## できること

| | |
|---|---|
| チャンネル → プロジェクトのルーティング | `channel_id` で判定。チャンネル名の変更で壊れない |
| 作業階層の切り替え | `cd:` でプロジェクト配下の階層を選ぶ。階層ごとの CLAUDE.md と skills が効く |
| 未登録チャンネルの拒否 | 知らないチャンネルでは Claude Code を起動しない |
| 権限プロファイル | プロジェクトごとに許可ツールを切り替え |
| セッション継続 | Slack スレッド = Claude Code セッション |
| 経過表示 | `chat.update` で1つのメッセージを書き換える |
| 自動コミット | 実行前後の差分を見て、この実行で変わった分だけコミット |
| push と PR リンク | `claude/<スレッド>` ブランチへ。既定ブランチへは push しない |
| CI 結果通知 | GitHub Actions の結果をポーリングしてスレッドへ |
| 多重起動の防止 | PID ロック。複数動くとイベントが散って記録が壊れる |
| 再送イベントの冪等化 | `event_id` で重複実行を防ぐ |
| 同一スレッドの直列化 | 同じセッションを2プロセスで更新しない |

## セットアップ

1. [`slack-bridge/SETUP.md`](slack-bridge/SETUP.md) — 最短の起動手順
2. [`slack-bridge/README.md`](slack-bridge/README.md) — 設計と運用の詳細
3. [`CLAUDE.md`](CLAUDE.md) — 元の仕様

必要なもの: Python 3.13 / Claude Code CLI / Slack ワークスペースの管理権限。

## 構成

```
slack-bridge/app/
├── main.py            エントリポイント（Socket Mode 接続、ログ、PID ロック）
├── slack_handler.py   受け付けるかどうかの判断
├── task_flow.py       実行フロー（Claude 起動 → コミット → push → CI）
├── file_flow.py       ファイル受け渡しのフロー
├── claude_runner.py   claude CLI の起動と JSON 解析
├── task.py            BridgeContext / TaskRequest
├── task_mode.py       依頼文先頭のディレクティブ解釈
├── workdir.py         スレッドごとの作業階層（cd:）
├── artifacts.py       出力ファイルの検出・一覧・受け渡し
├── share_queue.py     _share/ 送信待ち行列
├── inbox.py           Slack 添付の受け取り
├── git_ops.py         差分検知・コミット・push
├── github_ops.py      GitHub Actions の結果取得
├── formatting.py      mrkdwn 整形とメッセージ分割
├── reply.py           スレッドへの返信口
├── config.py          projects.yaml と .env の読み込み
├── project_router.py  channel_id → プロジェクト
├── session_store.py   スレッド ⇄ セッションの記録
├── concurrency.py     イベント冪等化・スレッドロック
├── single_instance.py 多重起動の防止
├── progress.py        経過表示
├── paths.py           同名ファイルを潰さない配置
└── console.py         Windows コンソールの UTF-8 化
```

`config/projects.yaml` にチャンネルと作業ディレクトリの対応を書く。プロジェクトの
追加はこのファイルへの追記だけで済み、コードは触らない。

## テスト

```bash
cd slack-bridge
./venv/Scripts/python.exe -m pytest tests/ -q     # 344 件
./venv/Scripts/python.exe -m ruff check app/ tests/ --select F
```

`tests/test_slack_handler.py` は特性化テスト。内部構造ではなく外から観測できること
（Slack へ何を投稿したか、Claude をどんな引数で起動したか、ファイルシステムに何を
残したか）だけを見るので、内部を作り替えても通り続ける。

## 前提として承知しておくこと

このブリッジは **Slack から自宅 PC 上の任意のコマンドを実行できる**。既定の権限
プロファイルは全ツール許可（`--permission-mode bypassPermissions`）で、PC の前で
`claude` を起動したときと同じことができる。Slack から依頼する人と PC を操作する人が
同一だという前提の設定であり、取り消し手段は git だけ。

`_inbox/` に降ろしたファイルも Claude が読める場所に置かれる。**チャンネルに人を
追加すると前提が変わる**ので、そのときは `config/projects.yaml` の
`permission_profiles` を絞ること。

## 状態

Socket Mode 受信からコミット、ファイルの双方向受け渡しまでは実機で動作確認済み。
**push / PR リンク / CI 結果通知はテストはあるが実機で通していない**（リモートと
`GITHUB_TOKEN` を設定していなかったため）。
