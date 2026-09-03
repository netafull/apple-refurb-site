#!/usr/bin/env python3
"""更新が失敗したことを ntfy (https://ntfy.sh/) で知らせる。

取得やデプロイに失敗したとき、サイトは前回の内容のまま据え置かれる。
1回なら実害はないが、止まったことに気づけないと古い在庫情報が何時間も
放置される。特に Cloud Scheduler の PAT が期限切れになると3サイトとも
GitHub Actions 側にエラーを残さないまま静かに更新停止するため、
失敗したことだけは必ず手元に届くようにしておく。

新規入荷の下書き作成の通知は post_to_wordpress.py が送る(下書きは
デプロイ後に作られるので、あちらはその場で送って問題ない)。

トピック名は GitHub Secrets (NTFY_TOPIC) で管理し、リポジトリには書かない
(公開リポジトリなので、トピック名が漏れると誰でも通知を送りつけられる)。

使い方:
  python3 scripts/notify.py --failure

必要な環境変数:
  NTFY_TOPIC : ntfyのトピック名 (未設定なら何もしない)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SITE_NAME = "林檎ポチ"


def failure_notification() -> dict:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return {
        "title": f"{SITE_NAME}: 更新に失敗しました",
        "message": "サイトは前回の内容のままです。実行ログを確認してください。",
        "click": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
    }


def send(topic: str, notification: dict) -> None:
    payload: dict[str, str] = {
        "topic": topic,
        "title": notification.get("title", ""),
        "message": notification.get("message", ""),
    }
    if notification.get("click"):
        payload["click"] = notification["click"]
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def main() -> int:
    if "--failure" not in sys.argv[1:]:
        print("usage: notify.py --failure", file=sys.stderr)
        return 1

    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return 0
    # 既に失敗している実行の後始末なので、通知の失敗でさらに落とさない
    try:
        send(topic, failure_notification())
        print("更新失敗を通知しました")
    except (urllib.error.URLError, OSError) as e:
        print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
