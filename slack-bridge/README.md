# Slack Bridge

Slack のチャンネルをインターフェースに、自宅 Windows PC 上の Claude Code へ
開発タスクを渡すブリッジ。仕様は `../CLAUDE.md`。

実装済みのスコープ:

**Phase 1** — Socket Mode 受信 → event_id 冪等化 → channel_id ルーティング →
未登録チャンネルの拒否 → 調査プロファイルで `claude -p` 実行 → mrkdwn 整形・分割
→ スレッド返信。

**Phase 2** — Slack スレッド = Claude Code セッションの継続、`chat.update` による
経過表示、同一スレッドの直列化。

**Phase 3** — コード変更の解禁（既定は実装モード。`調査:` で読み取り専用へ降格）、
Bridge 側での変更検知とコミット。

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
| OAuth & Permissions → Bot Token Scopes | `app_mentions:read`, `chat:write`, `files:write`, `files:read`, `channels:read`, `groups:read` |
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

プロファイルは `config/projects.yaml` の `permission_profiles` で定義する。

| プロファイル | 内容 |
|---|---|
| `implement`（既定） | `--tools` を渡さないので **Bash を含む全ツール**が使える。`--permission-mode bypassPermissions` により承認も求めない。PC で `claude` を起動したときと同じ |
| `investigate` | `--tools "Read,Grep,Glob,WebSearch,WebFetch"`。Bash / Edit / Write を含めないため、コード変更もシェル実行も構造的に起こりえない |

既定が `implement` なのは、Slack から依頼する人と PC で操作する人が同一人物であり、
インターフェースの違いで権限が変わるのは不便なため。この設定は
`--dangerously-skip-permissions` と実質的に同等で、CLAUDE.md §20 の制限を意図的に
解除している。**取り消し手段は git だけ**なので、対象は git 管理下に置くことを勧める。

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

既定は各プロジェクトの `profile`（現在はすべて `implement`）。前置き無しで依頼すれば
そのまま編集もシェル実行もできる。

```text
@ClaudeCode
README のタイポを直して
```

### 読み取り専用で走らせる

触らずに調べるだけにしたいときは、依頼文の**先頭**に降格キーワードを置く。

```text
@ClaudeCode
調査: 構成を教えて
```

キーワードは `調査:` / `調査：` / `read:` / `/read:` / `/investigate:` / `investigate:`。
**末尾のコロンは必須**（半角・全角どちらでも可）。行頭のみを見るので、本文中の
「〜の調査について」には反応しない。昇格側の `実装:` / `impl:` なども従来どおり使えるが、
既定が `implement` になったので通常は書く必要がない。両方を書いた場合は安全側
（読み取り専用）に倒す。

プロジェクトに `allow_implement: false` を設定すると、そのチャンネルは調査専用になり
昇格キーワードを拒否する。

### git の扱い

Claude は Bash を持つので自分で git を操作できるが、Bridge 側（`app/git_ops.py`）も
実行の前後で差分を見て自動コミットする。Claude が自分でコミットまで済ませた場合、
Bridge から見た差分は無いので二重コミットにはならない。

### コミットの範囲

実行の前後で `git status --porcelain` を比較し、**この実行で変化したファイルだけ**を
コミットする。実行前から未コミットだったファイルは利用者の作業中のものとみなして
触らず、その旨を Slack に注記する。

### git 管理外のプロジェクト

git リポジトリでないプロジェクトでも実装モードで実行する（`business-contest` が該当）。
ただし差分の記録も自動コミットも行わないため、**変更を取り消す手段が無い**。
安全網が要るなら対象ディレクトリで `git init` すれば、以降は自動コミットの対象になる。

## 作業階層（`cd:`）

Claude Code は起動した場所から `.claude/` を探す。プロジェクト内に開発ルールが複数
階層あると、どこで起動するかで読まれる CLAUDE.md と skills が変わる。

```text
propose/
├── .claude/CLAUDE.md, skills/
└── movie/
    └── .claude/CLAUDE.md, skills/
```

```text
@ClaudeCode
ls

@ClaudeCode
cd: movie
```

`cd:` のキーワードは `cd:` / `dir:` / `/cd:` / `階層:` / `移動:`。引数なしで現在の階層と
`.claude/` を持つ階層の一覧を返し、`cd: .` で直下に戻す。**スレッド単位で持続**し、
`state/workdirs.json` に記録するのでブリッジを再起動しても残る。

`ls` のキーワードは `ls` / `list` / `一覧` / `中身`。**コロンを省ける**（一番よく打つ形なので
強制しない）。引数なしで現在の作業階層、`ls: movie` で移動せずにその中を見る。
ディレクトリには `.claude/` を持つかを添えるので、一覧から移動先を判断できる。
`venv` や `node_modules` のような移動先にならない場所と `.claude` 自身は隠す。
件数が多い場合は**ディレクトリを優先して残す**（移動先を探すのが目的なので）。

どちらも `共有:` と同じくブリッジが直接処理するため、Claude Code は起動しない。

### プロジェクト配下に限る

`..`・絶対パス・存在しない階層・ファイルは拒否する。resolve 後にもう一度境界を
確認しているので、シンボリックリンク経由でも外へ出られない。

ここに Slack からの入力がそのまま通ると、`config/projects.yaml` による範囲指定の
歯止めが効かなくなる（CLAUDE.md §15 の「未登録チャンネルでは実行しない」と同じ趣旨）。

### git はリポジトリルートで動かす

`git status --porcelain` は**リポジトリルート基準**でパスを返すのに、`git add` の
パススペックは**cwd 基準**で解釈される。サブディレクトリで git を動かすと

```
$ cd sub && git status --porcelain -uall
?? sub/b.txt
$ git add -- sub/b.txt
fatal: pathspec 'sub/b.txt' did not match any files
```

となって自動コミットが必ず失敗する。そのため `git_ops.repo_root()` でルートを求め、
git の操作と git 由来のパスの解決はすべてそこを基準にする。**「Claude が動く場所」と
「git が動く場所」は別物**として扱う。

プロジェクト直下とリポジトリルートが一致しない場合にも、これで噛み合う。

### セッションは階層ごとに分かれる

Claude Code のセッションは作業ディレクトリ単位で保存される。

```text
C--Users-yutob-work-env-business-contest                    → 4 セッション
C--Users-yutob-work-env-business-contest-BusinessStrategist → 3 セッション
```

階層を変えたのに同じ `session_id` を `--resume` しようとすると見つからず、文脈が
切れる。そこで `derive_session_id` の導出に階層を混ぜ、階層ごとに別の id にしている。
行って戻ってくればそれぞれの会話が続く。直下のときは従来と同じ id になるので、
既存スレッドの継続は壊れない。

### `_share/` と `_inbox/` は動かさない

階層を変えてもプロジェクト直下に置く。階層ごとに散ると `共有: 1` の番号と実体の
対応が追えなくなるため。下の階層で動いている Claude には `../_share/` のような
相対パスで伝える（素の `_share/` と伝えると Claude は自分の cwd の下に作ってしまい、
添付されない）。

## 出力ファイルの共有

Slack から依頼した場合、生成物は自宅PCの中にあるのでスマホからは見えない。
そこで二段構えにしている（`app/artifacts.py`）。

1. 実行後、この実行で出力されたファイルを一覧でスレッドへ出す（自動）
2. 欲しいものだけを番号で受け取る

```text
:paperclip: この実行で出力されたファイル
`1` `docs/report.md` （12.4 KB）
`2` `data/summary.csv` （3.1 KB）

受け取るには `共有: 1` のように番号を指定してください（`共有: all` で全部）。
```

```text
@ClaudeCode
共有: 1
```

キーワードは `共有:` / `share:` / `/share:` / `送って:` / `ちょうだい:`。番号のほか、
`共有: report` のようにファイル名の部分一致でも指定できる（スマホからパスを正確に
打たずに済ませるため）。複数指定は `共有: 1,3` や `共有: 1 3`。

### なぜ毎回添付しないか

実装タスクではソースが十数ファイル変わることがあり、それを全部送るとスレッドが
埋まって肝心の回答が読めなくなる。一覧だけなら数行で済み、欲しいものは後から
番号で取り出せる。

### 共有はClaude を起動しない

`共有:` はメンションを受けた時点でブリッジが直接処理する。Claude Code は走らないので
課金もセッション更新も発生せず、応答は即座に返る。

### 出力ファイルの検出

| プロジェクト | 方法 |
|---|---|
| git 管理下 | `git status --porcelain` の差分。`.gitignore` が効くのでビルド生成物や venv を拾わない |
| git 管理外 | mtime とサイズの走査。`venv` / `node_modules` / `__pycache__` / `.git` などは自前の除外リストで外す |

調査モードではファイルを書けないので、一覧は実装モードのときだけ出る。

### 制限

- 1ファイル 50MB を超えるものは一覧に載るがアップロードしない
- 一覧は1スレッドあたり直近30件まで。実行するたびに置き換わる
- 番号はスレッド単位で `state/artifacts.json` に記録され、ブリッジを再起動しても有効

## Claude から Slack へ送る（`_share/`）

利用者が番号で選ぶのとは別に、**Claude 自身が「これを渡したい」と判断したもの**を
送る経路がある。`_share/` に置けば、一覧を挟まずそのままスレッドへ添付される。

```text
@ClaudeCode
売上データを集計して、結果をグラフ付きのレポートにして
```

Claude が `_share/report.md` を作れば、実行完了と同時に添付される。

### なぜ Claude に Slack を触らせないか

`claude_runner.py` の `subprocess.run` は `env=` を渡していないため、ブリッジの環境変数
（`SLACK_BOT_TOKEN` を含む）が Claude の子プロセスへ継承される。したがって Claude は
技術的には curl で Slack API を直接叩けるが、その経路は使わない。

- 送信先（channel_id / thread_ts）を Claude に教える必要がない
- Claude 側から見れば「ファイルを書く」だけで済み、API も認証も出てこない
- 失敗がブリッジ側のログと Slack 返信に一本化される

Claude は `_share/` の存在をプロンプトの前置きで知る（`_build_prompt`）。実装モードの
ときだけ付けている。調査モードではそもそも書き込めないため。

### `_share/` の扱い

転送用の置き場であってプロジェクトの成果物ではないので、次の2つから外している。

- **自動コミット** — リポジトリが共有ファイルで汚れないようにする
- **出力ファイル一覧** — 自動添付済みのものが一覧にも出ると二重になる

### `_share/` は送信の待ち行列

`_share/` にあるもの = まだ送っていないもの、と扱う。送れたら `_share/sent/` へ
移して行列から外す。

```text
_share/
├── report.md        ← 次の実行で送られる
└── sent/
    └── summary.md   ← 送信済み。もう送られない
```

この形にしている理由は3つ。

- **重複しない** — 送ったものは行列から居なくなるので、二度届かない
- **失敗が自動で回復する** — 送れなかったファイルは残るので、次の実行で再試行される
- **状態が目で見て分かる** — 記録ファイルではなくディレクトリが状態そのもの

同名のファイルを繰り返し送った場合は `summary-1.md` のように退避先で採番するので、
履歴も潰れない。

### 退避分の保持期間

`_share/sent/` は `share_retention_days`（既定 7 日）を過ぎたものを自動で掃除する。
放っておくと膨らむため。`0` にすると退避せず、送信後すぐ削除する。

```yaml
runtime:
  share_retention_days: 7   # 0 = 送信後すぐ削除
```

即削除ではなく既定で数日残すのは、**`_share/` のファイルにコピー元があるとは限らない**
ため。Claude が成果物を最初から `_share/` に書くことがあり、その場合そこが唯一の実体に
なる。実際、最初の転送テストで置かれた3件のうち1件はコピー元が無かった。

ただし送信に成功していれば Slack 側には実体が残るので、掃除で失われるのは
「ローカルの控え」だけ。掃除は未送信のファイル（`_share/` 直下）には触らない。

以前は送信済みの内容ハッシュを `state/artifacts.json` に記録していたが、
(1) 記録の上限を超えると古いものが「未送信」に戻って再送される、
(2) 記録ファイルを失うと `_share/` の中身が全部もう一度届く、
という2つの重複経路があった。ディレクトリを状態にすれば、どちらも起こらない。

退避に失敗した場合（ファイルがロックされている等）は、次の実行で重複するため
Slack へ警告を出す。黙って二度送らない。

## Slack から受け取る（`_inbox/`）

メンションにファイルを添付すると、プロジェクト直下の `_inbox/` へ降ろしたうえで
Claude に場所を伝える。

```text
@ClaudeCode
[売上.csv を添付]
これを分析して
```

Claude が受け取る依頼文はこうなる。

```text
[Slack に添付されたファイルを次の場所へ保存しました]
- _inbox/売上.csv

これを分析して
```

本文を書かずに添付だけ送った場合は「内容を要約してください」として扱う。

### 保存時の扱い

| | |
|---|---|
| 保存先 | プロジェクト直下の `_inbox/`。プロジェクトのファイルに直接混ぜない |
| 同名ファイル | 上書きせず `report-1.md` のように退避する。スマホからは送り直しが起きやすいため |
| ファイル名 | パス区切りや `..` を潰して `_inbox/` の外へ出られないようにする |
| 上限 | 1ファイル 50MB。申告サイズと実体の両方で見る |
| 失敗 | 1件ずつ独立。1つ落とせなくても他は保存する |

### 受け取りに伴うリスク

`_inbox/` に降ろしたファイルは、Claude が Bash で読める場所に置かれる。**Slack に
ファイルを投げられる人は、このPCで実行される内容に影響を与えられる**ということになる。
1人のワークスペースなら実害は薄いが、チャンネルに人を追加する場合は前提が変わる。

### 必要なスコープ

`files:write`（送信）と `files:read`（受信）が必要。マニフェストには含めてあるが、
**既存の App には自動で追加されない**。[api.slack.com/apps](https://api.slack.com/apps) →
OAuth & Permissions → Bot Token Scopes に両方を追加し、Reinstall to Workspace を実行する。

どちらも、スコープが無いまま使うと原因と手順を Slack へ返す。`files:read` が無い場合、
Slack はエラーではなく HTML のログインページを返してくるため、Content-Type を見て
判定している（黙って壊れたファイルを保存しないため）。

## モデルの指定

依頼ごとに使うモデルを選べる。先頭にモデル名とコロンを置く。

```text
@ClaudeCode
opus: 設計を詰めて

@ClaudeCode
調査 haiku: ざっと見て
```

モード指定との順序は問わない（`haiku 調査:` でも同じ）。指定が無ければ
`config/projects.yaml` の `model`、それも未設定なら Claude Code の既定モデルで動く。

使えるモデルは `allowed_models`（既定は `opus` / `sonnet` / `haiku` / `fable`）。
**一覧に無い名前は実行前に弾いて Slack へ知らせる。** 打ち間違いを黙って既定モデルで
走らせると、指定したつもりの利用者が気づけないまま別のコストで課金されるため。

実行中のモデルは開始・経過・完了の各メッセージに表示される。

```text
:hourglass_flowing_sand: 処理を開始しました。
Project: *navigation-core* / :pencil2: 実装モード / :brain: haiku
```

### ディレクティブの解釈規則

先頭の「コロンまで」を見て、既知のトークン（実装キーワード・モデル名）だけで構成されて
いればディレクティブとして扱う。既知のトークンが1つも無ければ普通の文章なので、
`TODO: あとで見る` や `URL: https://... を調べて` が誤解釈されることはない。

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
