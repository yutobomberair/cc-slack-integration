# Slack Bridge

Slack のチャンネルをインターフェースに、自宅 Windows PC 上の Claude Code へ
開発タスクを渡すブリッジ。仕様は `../CLAUDE.md`。

実装済みのスコープ:

**Phase 1** — Socket Mode 受信 → event_id 冪等化 → channel_id ルーティング →
未登録チャンネルの拒否 → 調査プロファイルで `claude -p` 実行 → mrkdwn 整形・分割
→ スレッド返信。

**Phase 2** — Slack スレッド = Claude Code セッションの継続、`chat.update` による
経過表示、同一スレッドの直列化。

**Phase 3** — 二重の鍵によるコード変更の解禁、Bridge 側での変更検知とコミット。

**Phase 4** — スレッド専用ブランチへの push と PR 作成リンクの返信。

**Phase 5** — GitHub Actions の結果をポーリングし、依頼元スレッドへ通知。

## Socket Mode を使う理由

`CLAUDE.md` §16/§17 は「自宅 PC を HTTPS で公開して Events API を受ける」前提だが、
本実装は **Socket Mode** を採用している。Slack へ outbound の WebSocket を張るだけで
済むため、以下がすべて不要になる。

- ポート開放・Cloudflare Tunnel / ngrok の運用
- ngrok 無料版の URL 変動に伴う Event Subscriptions URL の貼り直し
- Signing Secret による署名検証（外部から叩ける受け口が存在しないため）

Phase 5 の CI 通知もポーリングで実装したため、**結局 HTTP 受け口は一度も必要にならなかった**。

## セットアップ

### 1. Slack App を作成

api.slack.com/apps → Create New App → From scratch。

| 設定箇所 | 内容 |
|---|---|
| Socket Mode | **Enable** |
| Basic Information → App-Level Tokens | Generate Token（scope: `connections:write`）→ `xapp-...` を控える |
| OAuth & Permissions → Bot Token Scopes | `app_mentions:read`, `chat:write`, `channels:read`, `groups:read` |
| Event Subscriptions | Enable → Subscribe to bot events に **`app_mention`** を追加 |
| Install to Workspace | 実行後 `xoxb-...` を控える |

対象チャンネルで `/invite @ClaudeCode` を実行して Bot を招待する。

### 2. 環境変数

```bash
cp .env.example .env
# SLACK_BOT_TOKEN と SLACK_APP_TOKEN を記入
./venv/Scripts/python.exe scripts/check_tokens.py   # 記入内容を検証
```

`.env` は `.gitignore` 済み（CLAUDE.md §16）。

### 3. 依存関係

```bash
cd slack-bridge          # すべてのコマンドはこのディレクトリで実行する
python -m venv venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

### 4. channel_id を設定

```bash
./venv/Scripts/python.exe scripts/list_channels.py
```

出力された ID を `config/projects.yaml` の `C_REPLACE_ME_*` と差し替える。
プレースホルダが残っていると起動時にエラーで停止する（誤ったチャンネルで動くより安全）。

### 5. 起動

```bash
./venv/Scripts/python.exe -m app.main
```

## Slack 抜きでの動作確認

channel_id を埋める前でも、Claude Code 側の動作だけ検証できる。

```bash
./venv/Scripts/python.exe scripts/smoke_test.py --list
./venv/Scripts/python.exe scripts/smoke_test.py navigation-core "主要なディレクトリと役割をまとめて。"
```

Slack へ投稿される文面（mrkdwn 変換・分割後）がそのまま標準出力に表示される。

## 権限設計（CLAUDE.md §20）

`--dangerously-skip-permissions` は使用していない。プロファイルは
`config/projects.yaml` の `permission_profiles` で定義する。

| プロファイル | 内容 |
|---|---|
| `investigate`（既定） | `--tools "Read,Grep,Glob,WebSearch,WebFetch"`。Bash / Edit / Write を含めないため、コード変更もシェル実行も構造的に起こりえない |
| `implement` | 上記に `Write,Edit,TodoWrite` を加え `--permission-mode acceptEdits`。**Bash は含めない**ので任意コマンドは実行できない。詳細は「コード変更（Phase 3）」を参照 |

全プロファイル共通で以下を付与する。

- `--permission-prompts none` — 承認が必要な操作は自動拒否。無人実行での挙動を確定させる
- `--max-budget-usd` — 1 実行あたりのコスト上限
- `--add-dir` を渡さない — `working_directory` の外へ出させない
- `--strict-mcp-config` — MCP サーバを読み込まない（下記）

拒否された操作は応答 JSON の `permission_denials` に載り、Slack の返信末尾に
:lock: 付きで併記される。

### MCP サーバを読み込まない

アカウント単位で有効な MCP サーバ（`claude.ai Claude Docs` など）は、`--tools` で
built-in ツールを絞っても読み込まれる。無人実行では承認する人がいないため
`--permission-prompts none` により自動拒否されるだけで、次のような副作用が出る。

- Claude が毎回それらを呼びに行って失敗し、ターンと課金を無駄にする
- Slack の返信末尾に `:lock: … mcp__claude_ai_Claude_Docs__read` という注記が並ぶ

`--mcp-config` を渡さずに `--strict-mcp-config` を付けることで、MCP サーバ無しの
状態になる。特定プロジェクトで MCP が必要になったら `--mcp-config` を併用する。

無効化するには `config/projects.yaml` で `strict_mcp_config: false`。

## 実装上の注意点

- **3 秒 ack**: `app_mention` ハンドラは即座に戻り、Claude Code の実行はワーカースレッドへ委譲する。ack が遅れると Slack がイベントを再送する。
- **冪等化**: 再送に備え `event_id` を記録し、重複イベントは無視する（`SeenEvents`）。これが無いと同一タスクが多重起動する。
- **エンコーディング**: `subprocess` の出力デコードとコンソール出力の双方で UTF-8 を明示する。Windows の既定 cp932 では日本語や em dash で例外になる。
- **プロンプトの受け渡し**: argv ではなく stdin。Windows のコマンドライン長制限とクォート処理を回避できる。
- **Slack の文字数上限**: 2900 文字で分割する。コードブロックの途中で切れる場合はフェンスを閉じ、次のメッセージで開き直す。

## テスト

```bash
./venv/Scripts/python.exe -m pytest tests -q
```

## 構成

```text
slack-bridge/
├── app/
│   ├── main.py            Socket Mode エントリポイント
│   ├── config.py          .env + projects.yaml の読み込みと起動時検証
│   ├── project_router.py  channel_id → プロジェクト（§4, §15）
│   ├── claude_runner.py   claude CLI のヘッドレス実行（§8, §20）
│   ├── slack_handler.py   イベント受信・冪等化・ワーカー委譲（§12, §19）
│   ├── formatting.py      Markdown → mrkdwn 変換と分割（§13）
│   ├── session_store.py   スレッド ⇄ セッションの対応（§11, §23.2）
│   ├── task_mode.py       調査／実装モードの判定（§20）
│   ├── git_ops.py         変更検知・コミット・push・PRリンク（§21）
│   ├── progress.py        chat.update による経過表示（§12）
│   ├── single_instance.py 多重起動の防止
│   ├── github_ops.py      Actions のポーリング（§22）
│   └── console.py         Windows コンソールの UTF-8 化
├── config/projects.yaml   チャンネル対応表と権限プロファイル
├── scripts/
│   ├── check_tokens.py    .env の2トークンを検証
│   ├── list_channels.py   channel_id の一覧取得
│   ├── smoke_test.py      Slack を介さない疎通確認
│   ├── smoke_session.py   セッション継続の疎通確認
│   ├── run_bridge.ps1     常駐用ラッパ（落ちたら再起動）
│   ├── install_task.ps1   タスクスケジューラへの登録／解除
│   ├── stop_bridge.ps1    プロセスツリーごと確実に停止
│   └── restart_bridge.ps1 停止 → 起動 → インスタンス数の確認
└── tests/
```

## セッション継続（Phase 2）

Slack のスレッドと Claude Code のセッションを 1:1 で対応させる（CLAUDE.md §11, §23.2）。

```text
session_id = uuid5(namespace, "<channel_id>:<thread_ts>")
```

同じスレッドからは常に同じ UUID が導出されるため、対応表を引かずに済む。初回は
`--session-id` でセッションを作り、2回目以降は `--resume` で継続する。「作成済みか
どうか」だけを `state/sessions.json` に記録しており、ブリッジを再起動しても継続できる。

記録と実態がずれた場合（セッションファイルが削除された等）は、新規セッションとして
自動的にやり直し、Slack には文脈が引き継がれなかった旨を添えて返す。応答を返せない
より、文脈を失ってでも返すほうが実用的なため。

同一スレッドへ連続で依頼が来た場合は `ThreadLocks` で直列化する。同じセッションを
2つのプロセスが同時に更新することを防ぐ。別スレッド同士は並行に走る。

無効化するには `config/projects.yaml` で `session_continuation: false`。

## 進捗表示（Phase 2）

開始時に投稿したメッセージを `chat.update` で 30 秒ごとに書き換える（CLAUDE.md §12）。
新しいメッセージを足すとスレッドが経過報告で埋まるため、1本を更新し続ける方式。

```text
:hourglass_flowing_sand: 処理を開始しました。        ← 受信直後
:hourglass_flowing_sand: 実行中… （1分30秒 経過）     ← 30秒ごと
:white_check_mark: 完了しました。（2分23秒 / 14ターン / $0.42）   ← 完了時
```

更新間隔は `progress_interval_seconds`（0 で無効）。Slack への更新が失敗しても
本処理は止めない。

## コード変更（Phase 3）

既定は常に調査モード。書き込みは**二重の鍵**が揃ったときだけ有効になる
（CLAUDE.md §20「コード変更 → MVPでは明示的に許可」）。

1. `config/projects.yaml` でそのプロジェクトに `allow_implement: true` があること
2. 依頼文の**先頭**に昇格キーワードが書かれていること

```text
@ClaudeCode
実装: README のタイポを直して
```

キーワードは `実装:` / `実装：` / `/impl` / `/implement` / `impl:`。行頭のみを見るので、
本文中の「〜の実装について」には反応しない。キーワードが無ければ常に調査モードで動く。

### Bash を渡さない

`implement` プロファイルのツールは `Read, Grep, Glob, WebSearch, WebFetch, Write,
Edit, TodoWrite` で、**Bash を含まない**。したがって Slack 経由でシェルが開く経路が
存在せず、実行できるのはファイル編集だけになる。

git の操作は Bridge 側（`app/git_ops.py`）が行う。この分担には2つの利点がある。

- 任意コマンドの実行経路が無い
- commit を Claude の判断に委ねないので「編集したが commit し忘れた」が起きない

### コミットの範囲

実行の前後で `git status --porcelain` を比較し、**この実行で変化したファイルだけ**を
コミットする。実行前から未コミットだったファイルは利用者の作業中のものとみなして
触らず、その旨を Slack に注記する。

### git 管理外のプロジェクト

`allow_implement` の設定に関わらず、git リポジトリでないプロジェクトでは実装モードを
拒否する。変更を戻す手段が無い状態で書き込むのは危険なため。

## push と Pull Request（Phase 4）

`allow_push: true` のプロジェクトでは、コミット後に **スレッド専用ブランチ** へ
push し、PR 作成リンクをスレッドへ返す。

```text
branch = claude/<YYYYMMDD>-<thread_ts のハッシュ6桁>
```

同じスレッドからは常に同じブランチ名になるので、スレッド内で依頼を重ねると1本の
ブランチにコミットが積まれ、PR も1つで足りる。セッション継続と同じ考え方。

### 既定ブランチには push しない

`git push origin HEAD:refs/heads/claude/<...>` の形で push する。**ローカルの
ブランチを切り替えないので作業ツリーに一切触れない**（エディタで開いたままでも安全）。

既定ブランチ（main / master）への push は、設定に関わらずコード側で拒否する。
merge も一切行わない（CLAUDE.md §20）。

### 認証

Git Credential Manager のキャッシュを使う。push 実行時は `GIT_TERMINAL_PROMPT=0`
と `GCM_INTERACTIVE=never` を渡し、**認証 GUI が出ないようにしている**。常駐プロセス
では GUI プロンプトが出ると復帰できずに固まるため、失敗させて Slack へ報告する。

### PR は gh CLI を使わない

`gh` を前提にすると導入と認証が増えるので、GitHub の compare URL を組み立てて渡す。
スマホから1タップで PR 作成画面が開く。

```text
https://github.com/<owner>/<repo>/compare/<base>...<branch>?expand=1
```

### ローカルの未 push コミットも一緒に上がる

push するのは現在の HEAD なので、ローカルにしか無かったコミットも同じブランチに
含まれる。これは意図した挙動（WIP のバックアップにもなる）だが、分かるように
「今回の変更 N 件に加え、ローカルに残っていた M 件のコミットも含まれます」と
件数付きで報告する。

### Slack への返信

```text
:floppy_disk: 変更をコミットしました
branch: `feat/x` / commit: `5eeae37b`
• `NOTE.md`
```NOTE.md | 1 +```
```diff …```
:information_source: 取り消す場合は `git revert 5eeae37b`。

:rocket: push しました → `claude/20260920-589cbd`
このブランチには今回の変更 1 件に加え、ローカルに残っていた 7 件のコミットも含まれます。
:link: Pull Request を作成する
:mag: GitHub でブランチを見る
:information_source: merge は行いません。
```

### push しない条件

| 条件 | 挙動 |
|---|---|
| `allow_push: false` | コミットのみ。理由をスレッドへ記載 |
| リモート未設定（`propose`） | コミットのみ。理由をスレッドへ記載 |
| 認証失敗・ネットワーク断 | コミットは残る。エラー内容をスレッドへ記載 |

## CI の結果通知（Phase 5）

push したコミットの GitHub Actions を追跡し、**依頼元のスレッドへ**結果を返す
（CLAUDE.md §22）。

```text
:rocket: push しました → `claude/20260921-abc123`
:link: Pull Request を作成する
        ↓ （CI 完了後、同じスレッドへ追って投稿）
:white_check_mark: *CI 成功*
:white_check_mark: `test` — success <ログを見る>
```

### Webhook ではなくポーリング

GitHub の Webhook で受けるには自宅 PC を HTTPS で公開する必要があり、Socket Mode で
不要にしたトンネルが復活してしまう。ポーリングなら **outbound の HTTPS だけ**で済み、
公開ポートゼロという性質を保てる。

返信先の点でも優れている。Bridge は「どのスレッドがどのコミットを push したか」を
知っているので結果を**元のスレッドへ**返せる。Actions から Slack の Incoming Webhook を
直接叩く方式では固定チャンネルにしか送れず、スレッドから切り離される。

### 応答をブロックしない

CI は分単位でかかるため、コミットと push の報告を先に返し、**CI の結果は別スレッドで
待って追って投稿する**。ワーカー（`max_workers: 1`）を占有しないので、待っている間も
他のチャンネルの依頼は動く。

### GitHub トークン

`.env` の `GITHUB_TOKEN` に Personal Access Token を設定する。**未設定なら CI 通知だけ
スキップし、他の機能はそのまま動く。**

| 種別 | 必要な権限 |
|---|---|
| fine-grained | 対象リポジトリ + Repository permissions > Actions: Read-only |
| classic | `repo` スコープ |

プライベートリポジトリの run を読むには必須。`scripts/check_tokens.py` で検証できる。

### 待たない条件

| 条件 | 挙動 |
|---|---|
| `GITHUB_TOKEN` 未設定 | CI 通知なし |
| `ci_wait_seconds: 0` | CI 通知なし |
| リモートが GitHub でない | CI 通知なし |
| push から 60 秒経っても run が無い | 「対応する Actions はありません」と返す |
| `ci_wait_seconds` 超過 | 「まだ終わっていません」と現状を返す |

### 対象リポジトリ側の CI

navigation-core には `.github/workflows/test.yml` を追加済み（push / PR で pytest を
実行）。依存関係は `management/requirements.txt` と `requirements-dev.txt` の2つで
足りることをクリーンな仮想環境で確認している（334 passed）。

## 常駐運用（Windows）

タスクスケジューラでログオン時に自動起動し、落ちたら監視スクリプトが再起動する。
追加のインストールは不要。

### 登録

```powershell
cd C:\Users\yutob\work_env\slack_integration\slack-bridge
.\scripts\install_task.ps1
```

登録と同時に起動する。`-NoStart` を付ければ登録のみ、`-Uninstall` で登録解除。

### なぜサービス化しないか

Claude Code の認証情報は `%USERPROFILE%\.claude\.credentials.json` にある。
SYSTEM やサービス用アカウントで動かすと `USERPROFILE` が変わってこのファイルを
見つけられず、**すべてのタスクが認証エラーになる**。実行ユーザーのまま動かすことが
必須条件なので、タスクスケジューラで十分であり、NSSM 等を入れる利点がない。

### 様子を見る（tmux の attach に相当）

コンソールを持たないため、ログの追尾が唯一の観測手段になる。

```powershell
# ブリッジ本体のログ
Get-Content -Wait -Tail 30 .\logs\bridge.log

# 監視スクリプトのログ（起動・再起動の記録だけ）
Get-Content -Wait -Tail 30 .\logs\supervisor.log
```

`logs/bridge.log` は 5MB × 5世代でローテートする。

### 操作

```powershell
.\scripts\restart_bridge.ps1          # 再起動（設定やコードを変えたらこれ）
.\scripts\stop_bridge.ps1             # 停止
Start-ScheduledTask -TaskName SlackBridge   # 起動
Get-ScheduledTask   -TaskName SlackBridge   # 状態確認
.\scripts\install_task.ps1 -Uninstall      # 登録解除
```

### Stop-ScheduledTask を直接使わない

`Stop-ScheduledTask` は監視ラッパ（powershell.exe）しか終了させず、**その子の
python プロセスが生き残る**。生き残ったインスタンスは Slack への接続を保ったまま
なので、次に起動すると複数インスタンスが並走する。実際にこれで3インスタンスが
同時に動き、次の不具合が出た。

* イベントがどのインスタンスに届くか不定になる
* セッション記録が食い違い `Session ID ... is already in use` になる
* Claude Code が二重に走り、課金も倍になる

対策として3段構えにしている。

1. `stop_bridge.ps1` がプロセスツリーごと落とす
2. 起動時に `state/bridge.pid` のロックを取り、2つ目は起動を拒否する
   （終了コード 2 なので監視スクリプトも再起動を試みない）
3. `SessionStore` はファイルの更新時刻を見て読み直す

なお venv の `python.exe` は本体を再実行するシムなので、1インスタンスでも
`python.exe` が2プロセス見える。インスタンス数は `state/bridge.pid` で数えること。

### 監視スクリプトの再起動判定

`scripts/run_bridge.ps1` は終了コードで挙動を変える。設定ミスで無限に再起動を
繰り返さないようにするため。

| 終了コード | 意味 | 挙動 |
|---|---|---|
| 0 | 意図的な停止（Ctrl+C） | 再起動しない |
| 2 | 設定・環境の不備 | 再起動しない（人手が必要） |
| その他 | クラッシュ | 5秒→10秒→…→最大300秒 とバックオフして再起動 |

120秒以上稼働できた場合は一時的な障害とみなし、待ち時間をリセットする。
なお Slack との一時的な切断は Bolt 側が自動再接続するため、監視スクリプトの
出番はプロセスごと落ちた場合に限られる。

### PC のスリープ

スリープするとブリッジも止まる。常時稼働させるなら無効化しておく。

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

### PowerShell スクリプトの文字コード

`.ps1` は **UTF-8 BOM 付き** で保存すること。Windows PowerShell 5.1 は BOM の無い
UTF-8 を ANSI（cp932）として読むため、日本語が壊れて構文エラーになる。

## 次フェーズ

- **Phase 6**: より高度な自律開発フロー（CLAUDE.md §25）
