"""``slack_handler`` の振り分けと実行フローの特性化テスト。

このファイルの目的は「今の挙動を固定すること」。``handle_app_mention`` と
``_run_task`` は合計300行近くあるのに直接のテストが無く、分解や引数の整理を
しても壊れたことに気づけなかった。リファクタリングの前にここで挙動を釘付けする。

したがって、ここで検証するのは内部構造ではなく**外から観測できること**だけ。

* Slack へ何を投稿したか
* Claude をどんな引数で起動したか（そもそも起動したか）
* ファイルシステムに何を残したか

内部の関数名や引数の渡し方が変わってもこのテストは通り続けるべきで、
通らなくなったらそれは挙動が変わったということ。
"""

import dataclasses
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from slack_bolt import App

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import claude_runner, inbox, slack_handler  # noqa: E402
from app.claude_runner import ClaudeResult  # noqa: E402
from app.config import PermissionProfile, Project, Runtime, Settings  # noqa: E402


# --------------------------------------------------------------------------
# 足場
# --------------------------------------------------------------------------

class FakeClient:
    """Slack の代わり。投稿・更新・アップロードを記録するだけ。"""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.uploads: list[dict] = []
        self._ts = 0

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        self._ts += 1
        return {"ts": f"{self._ts}.0"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ts": kwargs.get("ts")}

    def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)

    @property
    def texts(self) -> list[str]:
        return [p.get("text", "") for p in self.posts]

    def said(self, needle: str) -> bool:
        return any(needle in t for t in self.texts)


@pytest.fixture(autouse=True)
def offline_bolt(monkeypatch):
    """Bolt は起動時に auth.test を叩く。テストでは通信させない。

    本番ではこの検証が「トークンの取り違え」を早期に知らせてくれるので、
    production 側は触らずテスト側だけ差し替える。
    """
    def build(**kwargs):
        return App(signing_secret="test", token_verification_enabled=False, **kwargs)

    monkeypatch.setattr(slack_handler, "App", build)


@pytest.fixture
def project_dir(tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir()
    return cwd


@pytest.fixture
def settings(tmp_path, project_dir):
    return Settings(
        bot_token="xoxb-test",
        app_token="xapp-test",
        projects={
            "demo": Project(
                key="demo",
                name="Demo",
                channel_id="C111",
                working_directory=project_dir,
                profile="implement",
                allow_implement=True,
            )
        },
        profiles={
            "implement": PermissionProfile(key="implement"),
            "investigate": PermissionProfile(key="investigate", tools=["Read"]),
        },
        runtime=Runtime(
            # 記録ファイルは必ず tmp_path に向ける。既定のままだと本物の
            # state/ を書き換えてしまい、テスト間で状態が漏れる（実際に漏れた）。
            session_store_path=tmp_path / "sessions.json",
            artifact_store_path=tmp_path / "artifacts.json",
            workdir_store_path=tmp_path / "workdirs.json",
            progress_interval_seconds=0,  # 進捗スレッドを起こさない
        ),
    )


class SyncExecutor:
    """``submit`` をその場で実行する差し替え用の executor。

    テストを決定的にするため。本物のスレッドプールだと完了待ちが必要で、
    待ち方を間違えると「まだ動いている途中」を検証してしまう。
    並行性そのものを見るテスト（直列化）は本物を使うので、ここは
    ``dispatch`` を使うテストにだけ効かせる。
    """

    def __init__(self, **kwargs) -> None:
        pass

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def dispatch(settings, monkeypatch):
    """``app_mention`` ハンドラを呼ぶ関数を返す。ワーカーは同期実行される。"""
    monkeypatch.setattr(slack_handler, "ThreadPoolExecutor", SyncExecutor)
    app = slack_handler.create_app(settings)
    handler = app._listeners[0].ack_function

    def _dispatch(client, text="依頼", files=None, channel="C111", event_id="Ev1"):
        handler(
            body={"event_id": event_id},
            event={
                "channel": channel,
                "ts": "100.0",
                "text": f"<@U999> {text}" if text else "<@U999>",
                **({"files": files} if files else {}),
            },
            client=client,
        )

    return _dispatch


@pytest.fixture
def ran(monkeypatch):
    """``claude_runner.run`` を差し替え、呼ばれた引数を記録する。

    ``ran.writes`` に (相対パス, 内容) を積んでおくと、Claude が実行中に
    そのファイルを作ったかのように振る舞う。出力ファイルの検出は「実行の前後で
    変化したか」で判断するので、実行より前に置いたファイルでは再現できない。
    """
    class Calls(list):
        """記録した呼び出し。``writes`` は実行中に作らせたいファイル。"""

        writes: list[tuple[str, str]]

    calls = Calls()

    def fake_run(**kwargs):
        calls.append(kwargs)
        for rel, body in calls.writes:
            target = Path(kwargs["working_directory"]) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return ClaudeResult(ok=True, text="やりました", num_turns=1, cost_usd=0.5)

    calls.writes = []
    monkeypatch.setattr(claude_runner, "run", fake_run)
    return calls


# --------------------------------------------------------------------------
# 受け付けない依頼
# --------------------------------------------------------------------------

def test_unknown_channel_does_not_run_claude(dispatch, ran):
    client = FakeClient()
    dispatch(client, channel="C_UNKNOWN")

    assert ran == []                      # CLAUDE.md §15
    assert client.said("C_UNKNOWN")       # channel_id を返す


def test_empty_request_does_not_run_claude(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="")

    assert ran == []
    assert client.said("依頼内容が空でした")


def test_resent_event_is_ignored(dispatch, ran):
    client = FakeClient()
    dispatch(client, event_id="Ev-same")
    first = len(ran)
    dispatch(client, event_id="Ev-same")

    assert len(ran) == first              # 2回目は走らない


def test_unknown_model_with_a_known_token_is_rejected(dispatch, ran):
    # 「調査」が既知なので、隣の gpt5 はモデル名の打ち間違いと判断される
    client = FakeClient()
    dispatch(client, text="調査 gpt5: 調べて")

    assert ran == []
    assert client.said("gpt5")


def test_unknown_word_alone_is_treated_as_plain_text(dispatch, ran):
    # 既知トークンが1つも無い先頭区間はディレクティブとみなさない。
    # 「TODO:」のような普通の文が誤解釈されないための仕様で、その代わり
    # 「gpt5:」単独は弾かれず既定モデルで走る（task_mode.py の設計判断）。
    dispatch(FakeClient(), text="gpt5: 調べて")

    assert len(ran) == 1
    assert ran[0]["model"] is None


def test_implement_keyword_without_permission_is_denied(settings, ran):
    locked = dataclasses.replace(settings.projects["demo"], allow_implement=False)
    settings = dataclasses.replace(settings, projects={"demo": locked})
    app = slack_handler.create_app(settings)
    client = FakeClient()
    app._listeners[0].ack_function(
        body={"event_id": "E"},
        event={"channel": "C111", "ts": "1.0", "text": "<@U999> 実装: 直して"},
        client=client,
    )
    app._slack_bridge_executor.shutdown(wait=True)

    assert ran == []
    assert client.said("許可されていません")


# --------------------------------------------------------------------------
# 受け付ける依頼
# --------------------------------------------------------------------------

def test_request_runs_claude_in_the_project_directory(dispatch, ran, project_dir):
    dispatch(FakeClient(), text="構成を教えて")

    assert len(ran) == 1
    assert ran[0]["working_directory"] == project_dir


def test_mention_is_stripped_from_the_prompt(dispatch, ran):
    dispatch(FakeClient(), text="構成を教えて")
    assert "<@U999>" not in ran[0]["prompt"]
    assert "構成を教えて" in ran[0]["prompt"]


def test_start_message_names_the_project(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調べて")
    assert client.said("Demo")


def test_result_is_posted_to_the_thread(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調べて")

    assert client.said("やりました")
    assert all(p.get("thread_ts") == "100.0" for p in client.posts)


def test_investigate_directive_selects_the_readonly_profile(dispatch, ran):
    dispatch(FakeClient(), text="調査: 見るだけ")
    assert ran[0]["profile"].key == "investigate"


def test_default_profile_is_the_project_default(dispatch, ran):
    dispatch(FakeClient(), text="直して")
    assert ran[0]["profile"].key == "implement"


def test_model_directive_is_passed_through(dispatch, ran):
    dispatch(FakeClient(), text="haiku: 軽く見て")
    assert ran[0]["model"] == "haiku"


def test_failure_is_reported_without_raising(dispatch, monkeypatch):
    monkeypatch.setattr(
        claude_runner, "run", lambda **k: ClaudeResult(ok=False, error="こわれた")
    )
    client = FakeClient()
    dispatch(client, text="直して")

    assert client.said("こわれた")


def test_crash_inside_the_worker_is_reported(dispatch, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("想定外")

    monkeypatch.setattr(claude_runner, "run", boom)
    client = FakeClient()
    dispatch(client, text="直して")     # 例外が外に漏れない

    assert client.posts                 # 何かは返す


# --------------------------------------------------------------------------
# ファイルの受け渡し
# --------------------------------------------------------------------------

def test_attachment_is_saved_and_told_to_claude(dispatch, ran, monkeypatch, project_dir):
    monkeypatch.setattr(
        inbox,
        "save_files",
        lambda files, cwd, token: [inbox.Saved("_inbox/a.csv", "a.csv", 10)],
    )
    client = FakeClient()
    dispatch(client, text="分析して", files=[{"name": "a.csv"}])

    assert client.said("_inbox/a.csv")            # 利用者へ報告
    assert "_inbox/a.csv" in ran[0]["prompt"]     # Claude へも伝える


def test_attachment_without_text_still_runs(dispatch, ran, monkeypatch):
    monkeypatch.setattr(
        inbox, "save_files", lambda files, cwd, token: [inbox.Saved("_inbox/a.csv", "a.csv", 10)]
    )
    dispatch(FakeClient(), text="", files=[{"name": "a.csv"}])

    assert len(ran) == 1                          # 空依頼として弾かれない


def test_share_directory_contents_are_attached(dispatch, ran, project_dir):
    share = project_dir / "_share"
    share.mkdir()
    (share / "out.md").write_text("成果物", encoding="utf-8")

    client = FakeClient()
    dispatch(client, text="やって")

    assert [u["title"] for u in client.uploads] == ["_share/out.md"]
    # 送れたものは待ち行列から外れる
    assert not (share / "out.md").exists()
    assert (share / "sent" / "out.md").exists()


def test_share_request_does_not_run_claude(dispatch, ran, project_dir):
    # まず一覧を作る
    ran.writes.append(("report.md", "報告"))
    dispatch(FakeClient(), text="作って")
    ran.clear()

    client = FakeClient()
    dispatch(client, text="共有: 1", event_id="Ev2")

    assert ran == []                              # Claude は起動しない
    assert client.uploads                         # ファイルは届く


def test_output_listing_is_posted_for_new_files(dispatch, ran, project_dir):
    ran.writes.append(("report.md", "報告"))
    client = FakeClient()
    dispatch(client, text="作って")

    assert client.said("report.md")
    assert client.said("共有: 1")                  # 取り出し方を案内する


def test_no_listing_when_nothing_changed(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調べるだけ")
    assert not client.said(":paperclip:")


# --------------------------------------------------------------------------
# 直列化
# --------------------------------------------------------------------------

def test_same_thread_requests_are_serialised(settings, monkeypatch):
    """同じスレッドの依頼が重なっても Claude を同時に走らせない。"""
    overlapping = []
    active = threading.Lock()

    def slow_run(**kwargs):
        got = active.acquire(blocking=False)
        overlapping.append(not got)
        if got:
            active.release()
        return ClaudeResult(ok=True, text="ok")

    monkeypatch.setattr(claude_runner, "run", slow_run)
    settings = dataclasses.replace(
        settings, runtime=dataclasses.replace(settings.runtime, max_workers=4)
    )
    app = slack_handler.create_app(settings)
    handler = app._listeners[0].ack_function

    for i in range(4):
        handler(
            body={"event_id": f"E{i}"},
            event={"channel": "C111", "ts": "100.0", "text": "<@U999> やって"},
            client=FakeClient(),
        )
    app._slack_bridge_executor.shutdown(wait=True)

    assert not any(overlapping)

# --------------------------------------------------------------------------
# 作業階層（cd:）
#
# Claude Code は起動時の cwd から .claude/ を探すので、どこで起動するかで
# 読まれる CLAUDE.md と skills が変わる。cd: がそれを切り替えられること、
# そして切り替えても git とファイル受け渡しが噛み合うことを確かめる。
# --------------------------------------------------------------------------

@pytest.fixture
def nested(project_dir):
    """propose と同じ形。直下と movie に .claude を持つ。"""
    (project_dir / ".claude").mkdir()
    (project_dir / "movie" / ".claude").mkdir(parents=True)
    return project_dir


def test_cd_changes_where_claude_runs(dispatch, ran, nested):
    client = FakeClient()
    dispatch(client, text="cd: movie")
    assert ran == []                                  # Claude は起動しない
    assert client.said("movie")

    dispatch(FakeClient(), text="やって", event_id="Ev2")
    assert ran[0]["working_directory"] == (nested / "movie").resolve()


def test_default_runs_at_the_project_root(dispatch, ran, nested):
    dispatch(FakeClient(), text="やって")
    assert ran[0]["working_directory"] == nested


def test_cd_persists_across_requests_in_the_thread(dispatch, ran, nested):
    dispatch(FakeClient(), text="cd: movie")
    dispatch(FakeClient(), text="1回目", event_id="Ev2")
    dispatch(FakeClient(), text="2回目", event_id="Ev3")

    assert [r["working_directory"] for r in ran] == [(nested / "movie").resolve()] * 2


def test_cd_back_to_root(dispatch, ran, nested):
    dispatch(FakeClient(), text="cd: movie")
    dispatch(FakeClient(), text="cd: .", event_id="Ev2")
    dispatch(FakeClient(), text="やって", event_id="Ev3")

    assert ran[0]["working_directory"] == nested


def test_cd_outside_the_project_is_refused(dispatch, ran, nested):
    client = FakeClient()
    dispatch(client, text="cd: ../../etc")

    assert client.said("外へは移動できません")
    dispatch(FakeClient(), text="やって", event_id="Ev2")
    assert ran[0]["working_directory"] == nested      # 階層は変わっていない


def test_cd_to_missing_directory_suggests_candidates(dispatch, ran, nested):
    client = FakeClient()
    dispatch(client, text="cd: movei")                # 打ち間違い

    assert client.said("ありません")
    assert client.said("movie")                       # .claude を持つ階層を案内


def test_cd_without_argument_reports_current(dispatch, ran, nested):
    dispatch(FakeClient(), text="cd: movie")
    client = FakeClient()
    dispatch(client, text="cd:", event_id="Ev2")

    assert client.said("movie")
    assert ran == []


def test_start_message_shows_the_workdir(dispatch, ran, nested):
    dispatch(FakeClient(), text="cd: movie")
    client = FakeClient()
    dispatch(client, text="やって", event_id="Ev2")

    assert client.said("movie/")                      # どこで動いているか分かる


def test_share_dir_note_is_relative_to_the_workdir(dispatch, ran, nested):
    # _share/ はプロジェクト直下に固定なので、下の階層からは ../_share/
    dispatch(FakeClient(), text="cd: movie")
    dispatch(FakeClient(), text="やって", event_id="Ev2")

    assert "../_share/" in ran[0]["prompt"]


def test_share_dir_stays_at_the_project_root(dispatch, ran, nested):
    # 下の階層で動いていても、プロジェクト直下の _share/ から送る
    share = nested / "_share"
    share.mkdir()
    (share / "out.md").write_text("成果物", encoding="utf-8")

    dispatch(FakeClient(), text="cd: movie")
    client = FakeClient()
    dispatch(client, text="やって", event_id="Ev2")

    assert [u["title"] for u in client.uploads] == ["_share/out.md"]


def test_sessions_are_separate_per_workdir(dispatch, ran, nested):
    """階層ごとに別セッションになる。

    Claude Code のセッションは作業ディレクトリ単位で保存されるので、階層を
    変えたのに同じ session_id を resume しようとすると見つからず、文脈が切れる。
    """
    dispatch(FakeClient(), text="1回目")                            # 直下
    dispatch(FakeClient(), text="cd: movie", event_id="Ev2")
    dispatch(FakeClient(), text="2回目", event_id="Ev3")             # movie

    root_session, movie_session = ran[0]["session_id"], ran[1]["session_id"]
    assert root_session != movie_session
    assert ran[1]["resume"] is False                                 # movie では新規


def test_returning_to_a_workdir_resumes_its_own_session(dispatch, ran, nested):
    dispatch(FakeClient(), text="1回目")                             # 直下で開始
    dispatch(FakeClient(), text="cd: movie", event_id="Ev2")
    dispatch(FakeClient(), text="movie で", event_id="Ev3")
    dispatch(FakeClient(), text="cd: .", event_id="Ev4")
    dispatch(FakeClient(), text="直下に戻って", event_id="Ev5")

    assert ran[2]["session_id"] == ran[0]["session_id"]              # 同じ会話へ戻る
    assert ran[2]["resume"] is True


def test_commit_works_from_a_subdirectory(dispatch, ran, nested):
    """サブディレクトリからでも自動コミットが通る。

    git status --porcelain はリポジトリルート基準でパスを返すのに、git add の
    パススペックは cwd 基準で解釈される。サブディレクトリで git を動かすと
    `fatal: pathspec ... did not match any files` で必ず失敗するため、
    git はリポジトリルートで動かしている。その回帰を防ぐ。
    """
    subprocess.run(["git", "init", "-q", "."], cwd=nested, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init", "--allow-empty"], cwd=nested, check=True)

    dispatch(FakeClient(), text="cd: movie")
    ran.writes.append(("movie/new.py", "print(1)"))
    client = FakeClient()
    dispatch(client, text="作って", event_id="Ev2")

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=nested, capture_output=True, text=True, check=True,
    ).stdout
    assert "movie/new.py" in committed
    assert not client.said("did not match any files")

def test_ls_lists_the_current_level(dispatch, ran, nested):
    (nested / "metting").mkdir()
    client = FakeClient()
    dispatch(client, text="ls")

    assert ran == []                      # Claude は起動しない
    assert client.said("movie/")
    assert client.said("metting/")


def test_ls_follows_the_current_workdir(dispatch, ran, nested):
    (nested / "movie" / "blend").mkdir()
    dispatch(FakeClient(), text="cd: movie")
    client = FakeClient()
    dispatch(client, text="ls", event_id="Ev2")

    assert client.said("blend/")


def test_ls_with_an_argument_does_not_move(dispatch, ran, nested):
    (nested / "movie" / "blend").mkdir()
    client = FakeClient()
    dispatch(client, text="ls: movie")
    assert client.said("blend/")

    dispatch(FakeClient(), text="やって", event_id="Ev2")
    assert ran[0]["working_directory"] == nested   # 移動していない


def test_ls_outside_the_project_is_refused(dispatch, ran, nested):
    client = FakeClient()
    dispatch(client, text="ls: ../..")
    assert client.said("外へは移動できません")


# --------------------------------------------------------------------------
# usage は未登録チャンネルでも答える
#
# CLAUDE.md §15 は「未登録チャンネルで Claude Code を実行しない」という規定。
# usage は Claude を起動せず作業ディレクトリにも触らないので対象外にする。
# 通知が届くチャンネル（プロジェクト設定を持たない）で残量を聞けないと、
# 通知だけ来て確認できないことになる。
# --------------------------------------------------------------------------

def test_usage_answers_in_an_unregistered_channel(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="usage", channel="C_NOTIFY_ONLY")

    assert ran == []                                  # Claude は起動しない
    assert not client.said("プロジェクトが設定されていません")
    assert client.said("Claude Code Usage") or client.said("スナップショット")


def test_other_requests_are_still_refused_in_an_unregistered_channel(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="構成を調べて", channel="C_NOTIFY_ONLY")

    assert ran == []
    assert client.said("C_NOTIFY_ONLY")               # §15 はそのまま効く


def test_share_is_still_refused_in_an_unregistered_channel(dispatch, ran):
    # 共有はプロジェクトのファイルを扱うので、未登録チャンネルでは答えない
    client = FakeClient()
    dispatch(client, text="共有: 1", channel="C_NOTIFY_ONLY")
    assert client.said("C_NOTIFY_ONLY")


def test_cd_is_still_refused_in_an_unregistered_channel(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="cd: movie", channel="C_NOTIFY_ONLY")
    assert client.said("C_NOTIFY_ONLY")


def test_usage_works_in_a_registered_channel_too(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="usage")
    assert ran == []
    assert not client.said("プロジェクトが設定されていません")


# --------------------------------------------------------------------------
# モデルと推論の強さ
#
# 元の問題は「Slack でどのモデルが使われたか確認できない」こと。前置きを書かない
# 通常ケースではモデル名が空だった。要求値ではなく**実際に使われたモデル**を
# 応答から読んで出すのが肝。
# --------------------------------------------------------------------------

from app import model_prefs  # noqa: E402


def test_actual_model_is_reported_even_without_a_request(dispatch, monkeypatch):
    """指定しなくても、何で動いたか分かる（これが元の問題）。"""
    monkeypatch.setattr(
        claude_runner,
        "run",
        lambda **k: ClaudeResult(ok=True, text="ok", model="claude-opus-5",
                                 models=["claude-opus-5"]),
    )
    client = FakeClient()
    dispatch(client, text="直して")

    assert any("claude-opus-5" in u.get("text", "") for u in client.updates) or client.said(
        "claude-opus-5"
    )


def test_mismatch_between_request_and_actual_is_flagged(dispatch, monkeypatch):
    """「指定したつもりが効いていなかった」を黙って通さない。"""
    monkeypatch.setattr(
        claude_runner,
        "run",
        lambda **k: ClaudeResult(ok=True, text="ok", model="claude-opus-5",
                                 models=["claude-opus-5"]),
    )
    client = FakeClient()
    dispatch(client, text="haiku: 直して")

    everything = " ".join(client.texts + [u.get("text", "") for u in client.updates])
    assert "haiku" in everything and "claude-opus-5" in everything


# --- インライン指定（その依頼だけ） ---

def test_inline_effort_is_passed_to_the_cli(dispatch, ran):
    dispatch(FakeClient(), text="max: 難しい設計を詰めて")
    assert ran[0]["effort"] == "max"


def test_inline_model_and_effort_together(dispatch, ran):
    dispatch(FakeClient(), text="max opus: 直して")
    assert ran[0]["model"] == "opus"
    assert ran[0]["effort"] == "max"


def test_inline_order_does_not_matter(dispatch, ran):
    dispatch(FakeClient(), text="opus max: 直して")
    assert (ran[0]["model"], ran[0]["effort"]) == ("opus", "max")


def test_inline_effort_combines_with_a_mode(dispatch, ran):
    dispatch(FakeClient(), text="調査 low: ざっと見て")
    assert ran[0]["effort"] == "low"
    assert ran[0]["profile"].key == "investigate"


def test_unknown_effort_is_rejected_before_running(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調査 ultra: 見て")

    assert ran == []
    assert client.said("ultra")
    assert client.said("推論の強さ")      # 候補を案内する


# --- スレッドの設定（持続する） ---

def test_model_directive_persists_in_the_thread(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="model: sonnet")
    assert ran == []                      # Claude は起動しない
    assert client.said("sonnet")

    dispatch(FakeClient(), text="直して", event_id="Ev2")
    assert ran[0]["model"] == "sonnet"


def test_effort_directive_persists_in_the_thread(dispatch, ran):
    dispatch(FakeClient(), text="effort: max")
    dispatch(FakeClient(), text="直して", event_id="Ev2")
    assert ran[0]["effort"] == "max"


def test_effort_directive_warns_about_consumption(dispatch, ran):
    # 上げるとトークンを多く使うので、利用枠に響くことを伝える
    client = FakeClient()
    dispatch(client, text="effort: max")
    assert client.said("利用枠の消費")


def test_inline_beats_the_thread_setting(dispatch, ran):
    dispatch(FakeClient(), text="model: sonnet")
    dispatch(FakeClient(), text="haiku: 直して", event_id="Ev2")
    assert ran[0]["model"] == "haiku"


def test_thread_setting_can_be_cleared(dispatch, ran):
    dispatch(FakeClient(), text="model: sonnet")
    dispatch(FakeClient(), text="model: 既定", event_id="Ev2")
    dispatch(FakeClient(), text="直して", event_id="Ev3")
    assert ran[0]["model"] is None


def test_cd_does_not_wipe_the_model_setting(dispatch, ran, project_dir):
    """1つの記録にまとめた結果、階層を戻すとモデルが消える事故が起きうる。"""
    (project_dir / "movie").mkdir()
    dispatch(FakeClient(), text="model: sonnet")
    dispatch(FakeClient(), text="cd: movie", event_id="Ev2")
    dispatch(FakeClient(), text="cd: .", event_id="Ev3")
    dispatch(FakeClient(), text="直して", event_id="Ev4")

    assert ran[0]["model"] == "sonnet"


def test_model_without_argument_shows_the_current_setting(dispatch, ran):
    dispatch(FakeClient(), text="effort: high")
    client = FakeClient()
    dispatch(client, text="model", event_id="Ev2")

    assert ran == []
    assert client.said("high")             # いまの設定が分かる
    assert client.said("使えるモデル")


def test_unknown_model_setting_is_refused(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="model: gpt5")
    assert client.said("gpt5")

    dispatch(FakeClient(), text="直して", event_id="Ev2")
    assert ran[0]["model"] is None         # 設定されていない


# --- プロジェクトの既定 ---

def test_project_default_model_is_used(settings, ran, monkeypatch):
    import dataclasses

    project = dataclasses.replace(settings.projects["demo"], model="sonnet", effort="high")
    settings = dataclasses.replace(settings, projects={"demo": project})
    monkeypatch.setattr(slack_handler, "ThreadPoolExecutor", SyncExecutor)
    app = slack_handler.create_app(settings)
    app._listeners[0].ack_function(
        body={"event_id": "E"},
        event={"channel": "C111", "ts": "1.0", "text": "<@U999> 直して"},
        client=FakeClient(),
    )
    assert (ran[0]["model"], ran[0]["effort"]) == ("sonnet", "high")


def test_thread_setting_beats_the_project_default(settings, ran, monkeypatch):
    import dataclasses

    project = dataclasses.replace(settings.projects["demo"], model="sonnet")
    settings = dataclasses.replace(settings, projects={"demo": project})
    monkeypatch.setattr(slack_handler, "ThreadPoolExecutor", SyncExecutor)
    app = slack_handler.create_app(settings)
    handler = app._listeners[0].ack_function

    for i, text in enumerate(["model: haiku", "直して"]):
        handler(
            body={"event_id": f"E{i}"},
            event={"channel": "C111", "ts": "1.0", "text": f"<@U999> {text}"},
            client=FakeClient(),
        )
    assert ran[0]["model"] == "haiku"


# --- ディレクティブの解釈 ---

@pytest.mark.parametrize(
    "text,expected",
    [
        ("model: sonnet", ("model", "sonnet")),
        ("model sonnet", ("model", "sonnet")),
        ("モデル: sonnet", ("model", "sonnet")),
        ("model", ("model", "")),
        ("effort: max", ("effort", "max")),
        ("推論: max", ("effort", "max")),
        ("effort", ("effort", "")),
    ],
)
def test_pref_directive_parsing(text, expected):
    assert model_prefs.parse_directive(text) == expected


def test_pref_directive_ignores_plain_text():
    assert model_prefs.parse_directive("モデルの選び方を教えて") is None
    assert model_prefs.parse_directive("この仕組みについて\nmodel: の挙動") is None


@pytest.mark.parametrize("token", ["既定", "default", "reset", "-", "なし"])
def test_reset_tokens_clear_the_setting(token):
    assert model_prefs.validate("model", token, ["opus"]) == ""


def test_validate_rejects_unknown_values():
    assert model_prefs.validate("model", "gpt5", ["opus", "sonnet"]) is None


def test_validate_is_case_insensitive():
    assert model_prefs.validate("model", "OPUS", ["opus"]) == "opus"


# --------------------------------------------------------------------------
# 時間切れでも成果を失わない
#
# 以前は失敗・打ち切りで早期 return していたため、Claude が書いたファイルが
# コミットも一覧もされず「30分かけた作業が消えた」ように見えた。
# 打ち切られても実体はディスクに残っているので、必ず回収する。
# --------------------------------------------------------------------------

def test_timeout_still_commits_what_was_written(dispatch, ran, nested, monkeypatch):
    subprocess.run(["git", "init", "-q", "."], cwd=nested, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init", "--allow-empty"], cwd=nested, check=True)

    def timed_out(**kwargs):
        # 打ち切られる前に Claude がファイルを書いていた状況
        (Path(kwargs["working_directory"]) / "half_done.py").write_text("x", encoding="utf-8")
        return ClaudeResult(
            ok=False, error="制限時間を超えたため中断しました。", timed_out=True
        )

    monkeypatch.setattr(claude_runner, "run", timed_out)
    client = FakeClient()
    dispatch(client, text="重い作業をして")

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=nested, capture_output=True, text=True, check=True,
    ).stdout
    assert "half_done.py" in committed


def test_timeout_is_described_as_interrupted_not_failed(dispatch, ran, monkeypatch):
    monkeypatch.setattr(
        claude_runner,
        "run",
        lambda **k: ClaudeResult(ok=False, error="制限時間を超えました。", timed_out=True),
    )
    client = FakeClient()
    dispatch(client, text="重い作業をして")

    everything = " ".join(client.texts + [u.get("text", "") for u in client.updates])
    # やり直すのではなく「続きを頼む」のが正しい対処なので、そう案内する
    assert "時間切れ" in everything
    assert "続き" in everything


def test_timeout_still_lists_outputs(dispatch, ran, nested, monkeypatch):
    def timed_out(**kwargs):
        (Path(kwargs["working_directory"]) / "report.md" ).write_text("途中", encoding="utf-8")
        return ClaudeResult(ok=False, error="時間切れ", timed_out=True)

    monkeypatch.setattr(claude_runner, "run", timed_out)
    client = FakeClient()
    dispatch(client, text="レポートを作って")

    assert client.said("report.md")       # 番号で取り出せる


def test_timeout_still_attaches_share_dir(dispatch, ran, nested, monkeypatch):
    def timed_out(**kwargs):
        share = Path(kwargs["working_directory"]) / "_share"
        share.mkdir(exist_ok=True)
        (share / "partial.md").write_text("途中", encoding="utf-8")
        return ClaudeResult(ok=False, error="時間切れ", timed_out=True)

    monkeypatch.setattr(claude_runner, "run", timed_out)
    client = FakeClient()
    dispatch(client, text="資料を作って")

    assert [u["title"] for u in client.uploads] == ["_share/partial.md"]


def test_partial_commit_is_marked_in_the_history(dispatch, ran, nested, monkeypatch):
    """あとで履歴を見たときに「なぜ中途半端なのか」が分かるようにする。"""
    subprocess.run(["git", "init", "-q", "."], cwd=nested, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init", "--allow-empty"], cwd=nested, check=True)

    def timed_out(**kwargs):
        (Path(kwargs["working_directory"]) / "x.py").write_text("x", encoding="utf-8")
        return ClaudeResult(ok=False, error="時間切れ", timed_out=True)

    monkeypatch.setattr(claude_runner, "run", timed_out)
    dispatch(FakeClient(), text="作って")

    # text=True だけだと Windows の locale（cp932）で復号され、git の UTF-8 出力が
    # 壊れる。本番の app/git_ops.py は encoding="utf-8" を指定している。
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=nested,
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    assert "[中断]" in subject


def test_ordinary_failure_also_harvests(dispatch, ran, nested, monkeypatch):
    # 打ち切りでなくても、書かれたものは回収する
    def failed(**kwargs):
        (Path(kwargs["working_directory"]) / "oops.md").write_text("x", encoding="utf-8")
        return ClaudeResult(ok=False, error="API エラー")

    monkeypatch.setattr(claude_runner, "run", failed)
    client = FakeClient()
    dispatch(client, text="作って")

    assert client.said("oops.md")


def test_failure_without_changes_stays_quiet(dispatch, ran, nested, monkeypatch):
    # 何も書かれていなければ、回収は何もしない
    monkeypatch.setattr(
        claude_runner, "run", lambda **k: ClaudeResult(ok=False, error="即死")
    )
    client = FakeClient()
    dispatch(client, text="作って")

    assert not client.said(":paperclip:")
    assert client.uploads == []


def test_investigate_mode_harvests_nothing(dispatch, ran, nested, monkeypatch):
    # 調査モードでは書き込めないので回収する対象が無い
    monkeypatch.setattr(
        claude_runner, "run", lambda **k: ClaudeResult(ok=False, error="時間切れ", timed_out=True)
    )
    client = FakeClient()
    dispatch(client, text="調査: 構成を教えて")

    assert client.uploads == []


def test_timeout_limit_is_generous(settings):
    """「終わるまで待つ」方針。ただし完全に無くさない（max_workers=1 のため）。"""
    assert settings.runtime.timeout_seconds >= 7200
