"""Slack Bridge エントリポイント（Socket Mode）。

Socket Mode を使うため、自宅PCを外部公開する必要はない。Slack へは outbound の
WebSocket を張るだけで、受信用の公開ポート・HTTPS トンネル・署名検証はいずれも不要。

起動:
    python -m app.main
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# builtin（生ソケット実装）ではなく websocket-client 版を使う。builtin は Windows で
# 接続が約60秒ごとに落ち（WinError 10053/10054、SSL BAD_LENGTH）、復帰まで40秒ほど
# 無接続になる。その間に来たメンションは Socket Mode では再送されず取りこぼす。
from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler

from app import slack_handler
from app.console import force_utf8_stdio
from app.claude_runner import ClaudeNotFoundError, resolve_cli
from app.config import ConfigError, load_settings
from app.single_instance import AlreadyRunningError, InstanceLock


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bridge.log"
LOCK_FILE = BASE_DIR / "state" / "bridge.pid"


def configure_logging() -> None:
    """標準出力とファイルの両方へ出す。

    常駐させるとコンソールが無くなるため、ファイルログが唯一の観測手段になる
    （CLAUDE.md §14「原因をローカルログへ記録する」）。5MB × 5世代でローテートする。
    """
    force_utf8_stdio()
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # ファイルに書けなくても常駐自体は続行する
        root.warning("ログファイルを開けませんでした（標準出力のみ）: %s", exc)

    # Bolt の接続ログは冗長なので絞る
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)


# 終了コード。常駐時の監視スクリプトが「再起動すべきか」を判断するために使い分ける。
EXIT_OK = 0            # 意図的な停止（Ctrl+C など）→ 再起動しない
EXIT_CONFIG_ERROR = 2  # 設定・環境の不備 → 人手が要るので再起動しない
# それ以外（クラッシュ）→ 監視スクリプトが再起動する


def main() -> int:
    configure_logging()
    logger = logging.getLogger("slack_bridge")

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("設定エラー: %s", exc)
        return EXIT_CONFIG_ERROR

    try:
        cli_path = resolve_cli()
    except ClaudeNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    # 多重起動を防ぐ。複数動くとイベントが各インスタンスに散り、セッション記録と
    # スレッドロックが壊れる（実際に3インスタンス並走して不具合が出た）。
    lock = InstanceLock(LOCK_FILE)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR  # 監視スクリプトに再起動させない

    logger.info("=" * 70)
    logger.info("Slack Bridge 起動 (pid=%s)", os.getpid())
    logger.info("claude CLI: %s", cli_path)
    logger.info("ログファイル: %s", LOG_FILE)
    logger.info("登録プロジェクト:")
    for project in settings.projects.values():
        logger.info(
            "  %-18s channel=%s profile=%-11s cwd=%s",
            project.key,
            project.channel_id,
            project.profile,
            project.working_directory,
        )

    app = slack_handler.create_app(settings)
    handler = SocketModeHandler(app, settings.app_token)

    logger.info("Socket Mode で Slack に接続します（公開ポートは不要）")
    try:
        handler.start()
    except KeyboardInterrupt:
        logger.info("停止要求を受け取りました")
    finally:
        executor = getattr(app, "_slack_bridge_executor", None)
        if executor is not None:
            logger.info("実行中のタスクの完了を待っています…")
            executor.shutdown(wait=True)
        lock.release()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
