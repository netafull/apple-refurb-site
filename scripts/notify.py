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
  python3 scripts/notify.py --failure    更新が失敗したことを知らせる(失敗時専用)。
                                          連続失敗がFAILURE_ALERT_HOURSを超えた
                                          瞬間の1回だけ鳴るエッジトリガー方式
  python3 scripts/notify.py --recovered  連続失敗から復旧したことを知らせる

必要な環境変数:
  NTFY_TOPIC   : ntfyのトピック名 (未設定なら何もしない)
  GITHUB_TOKEN : --failure / --recovered で、直前までの実行結果をGitHub API
                 (workflow runs一覧)から参照するのに使う。ワークフロー側は
                 secrets.GITHUB_TOKEN(自動発行)を渡せば足りる。未設定や
                 API失敗時にどう倒すかはモード別のコメントを参照
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SITE_NAME = "林檎ポチ"

# 連続失敗が何時間続いたら通知するかの閾値。
#
# 根拠: 2026-09-09〜12にApple公式サイトが予約開始の負荷で503を返し続け、
# 林檎ポチだけで28通(うち15通は20:12〜翌03:12に30分おき)の失敗通知が
# 飛んだ。この障害は6〜7時間続いており、「毎回鳴らす」を「閾値を超えた
# ときだけ鳴らす」に変えるにも、実行間隔が30分(林檎)〜2時間(電書)と
# サイトごとに違うため、回数ではなく経過時間で揃える必要がある。
# 3時間なら林檎ポチ(30分間隔)でも高々6回の失敗で気づけ、かつ一時的な
# 503のチラつき程度では鳴らない
FAILURE_ALERT_HOURS = 3


def _current_run_click() -> str:
    """現在の実行のログURLを組み立てる(失敗通知・復旧通知で共通)。"""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def _status_unknown_notification() -> dict:
    """連続失敗の状況が判定できないときの通知。

    `--failure`側は「本当の障害を取りこぼすより1通多く鳴る方がマシ」と
    判断し、判定不能なら必ず通知する(`--recovered`とは逆の倒し方)。
    """
    return {
        "title": f"{SITE_NAME}: 更新状況を確認できません",
        "message": "連続失敗の状況を取得できなかったため念のため通知しています。実行ログを確認してください。",
        "click": _current_run_click(),
    }


def _streak_alert_notification(elapsed_hours: float, count: int) -> dict:
    """連続失敗がFAILURE_ALERT_HOURSを超えた回のみ送る通知。"""
    return {
        "title": f"{SITE_NAME}: 更新が止まっています",
        "message": (
            f"{elapsed_hours:.1f}時間、更新に失敗し続けています"
            f"(連続{count}回)。サイトは前回の内容のままです。"
        ),
        "click": _current_run_click(),
    }


def _recovered_notification(elapsed_hours: float, count: int) -> dict:
    """`--recovered`で送る復旧通知。"""
    return {
        "title": f"{SITE_NAME}: 更新が復旧しました",
        "message": f"{elapsed_hours:.1f}時間ぶりに更新されました(連続失敗{count}回)。",
        "click": _current_run_click(),
    }


def _extract_workflow_filename() -> str:
    """GITHUB_WORKFLOW_REFからワークフローのファイル名を取り出す。

    例: "netafull/apple-refurb-site/.github/workflows/update.yml@refs/heads/main"
    → "update.yml"。取れなければ実際のファイル名にフォールバックする。
    このリポジトリには update と pages-build-deployment の2ワークフローが
    あるが、ファイル名を指定して問い合わせるので自動的にupdateだけに絞られる
    """
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    path = ref.split("@", 1)[0]
    if not path:
        return "update.yml"
    return path.rsplit("/", 1)[-1] or "update.yml"


def _fetch_recent_runs() -> list[dict] | None:
    """GitHub APIから直前までの完了済みワークフロー実行を新しい順に取得する。

    ネットワークに触れるのはこの関数だけに閉じ込め、連続失敗の計算本体
    (`_failure_streak`)はダミーのrunsリストを直接渡してテストできる
    純粋関数にする。現在実行中の分(GITHUB_RUN_ID)はここで除外する。
    問い合わせに失敗した場合は「判定不能」を表すNoneを返す
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        return None

    workflow_file = _extract_workflow_filename()
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{workflow_file}/runs?status=completed&per_page=20&branch=main"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
        print(f"[warn] ワークフロー実行履歴の取得に失敗しました: {e}", file=sys.stderr)
        return None

    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    if not isinstance(runs, list):
        return None

    current_run_id = os.environ.get("GITHUB_RUN_ID", "")
    return [r for r in runs if isinstance(r, dict) and str(r.get("id")) != current_run_id]


def _parse_run_created_at(value: str) -> datetime:
    # GitHub APIは "2026-09-09T20:12:00Z" 形式(UTC)で返す
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _failure_streak(runs: list[dict]) -> dict | None:
    """直前までの実行結果(新しい順、現在の実行は除く)から、連続失敗の
    開始時刻などを計算する純粋関数。ネットワークには一切触れない。

    runsは `_fetch_recent_runs()` の戻り値と同じ形(GitHub APIの
    workflow_runs要素のリスト、新しい順)を想定しているが、検証時は
    ダミーのdictリストを直接渡せる。

    戻り値(判定できた場合):
      streak_start        : 連続失敗の開始時刻(最も古い連続失敗実行の
                             created_at)。直前の実行が失敗でない、または
                             実行履歴がまだ無い場合はNone
                             (=「連続失敗の開始を今とみなす」の合図)
      previous_run_time    : 直前の実行のcreated_at。実行履歴が無ければNone
      count_before_current : 現在の実行を含まない、直前までの連続失敗回数

    created_atが壊れていて時刻を解釈できない実行が混じっていた場合はNoneを
    返す(=判定不能。呼び出し元がモードに応じて非対称に倒す)
    """
    if not runs:
        return {"streak_start": None, "previous_run_time": None, "count_before_current": 0}

    try:
        previous_run_time = _parse_run_created_at(runs[0]["created_at"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None

    if runs[0].get("conclusion") != "failure":
        # 直前の実行が失敗でない = 今回(または今)が連続失敗の1回目
        return {
            "streak_start": None,
            "previous_run_time": previous_run_time,
            "count_before_current": 0,
        }

    # 新しい順にfailureが続く限り遡り、最後に更新された時刻が
    # 「連続失敗の開始時刻」になる
    streak_start = previous_run_time
    count = 0
    for run in runs:
        if run.get("conclusion") != "failure":
            break
        try:
            streak_start = _parse_run_created_at(run["created_at"])
        except (KeyError, ValueError, TypeError, AttributeError):
            return None
        count += 1

    return {
        "streak_start": streak_start,
        "previous_run_time": previous_run_time,
        "count_before_current": count,
    }


def _handle_failure_alert(topic: str) -> None:
    """`--failure`: 連続失敗がFAILURE_ALERT_HOURSを超えた瞬間の1回だけ鳴らす。

    判定: (今 - 連続失敗の開始時刻) >= 閾値 かつ
          (直前の実行時刻 - 連続失敗の開始時刻) < 閾値 のときだけ送る。
    これにより閾値をまたぐ回だけ鳴り、以降の失敗では鳴らない。
    長期化して20件の取得上限に達しても、30分間隔の林檎ポチでも
    20件=10時間で既に閾値をとうに超えているため、誤って再度鳴ることはない。

    状況が判定できないとき(API失敗・created_at破損)は、本当の障害を
    取りこぼすより1通多く鳴る方がマシという判断で必ず通知する
    """
    runs = _fetch_recent_runs()
    streak = _failure_streak(runs) if runs is not None else None

    if streak is None:
        try:
            send(topic, _status_unknown_notification())
            print("連続失敗の状況を判定できなかったため、念のため通知しました")
        except (urllib.error.URLError, OSError) as e:
            print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)
        return

    now = datetime.now(timezone.utc)
    if streak["streak_start"] is None:
        # 今回が連続失敗の1回目。開始を「今」とみなすので経過0時間となり、
        # この1回では絶対に閾値を超えない(=鳴らない)
        streak_start = now
        elapsed_before_hours = 0.0
    else:
        streak_start = streak["streak_start"]
        elapsed_before_hours = (streak["previous_run_time"] - streak_start).total_seconds() / 3600

    elapsed_now_hours = (now - streak_start).total_seconds() / 3600
    count = streak["count_before_current"] + 1  # 現在の実行を含めた連続失敗回数

    if elapsed_now_hours >= FAILURE_ALERT_HOURS and elapsed_before_hours < FAILURE_ALERT_HOURS:
        try:
            send(topic, _streak_alert_notification(elapsed_now_hours, count))
            print(f"連続失敗が{FAILURE_ALERT_HOURS}時間を超えたため通知しました(連続{count}回)")
        except (urllib.error.URLError, OSError) as e:
            print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)
    else:
        print(
            f"連続失敗{elapsed_now_hours:.1f}時間(連続{count}回)ですが、"
            f"閾値{FAILURE_ALERT_HOURS}時間をまたいだ回ではないため通知しません"
        )


def _handle_recovered(topic: str) -> None:
    """`--recovered`: 直前の実行が失敗していた場合にだけ復旧を知らせる。

    状況が判定できないときは黙る。誤って「復旧しました」を送る方が、
    1通多く鳴るより実害が大きいため`--failure`とは逆の倒し方にしている
    """
    runs = _fetch_recent_runs()
    streak = _failure_streak(runs) if runs is not None else None

    if streak is None:
        print("連続失敗の状況を取得できなかったため、復旧通知はスキップします")
        return

    if streak["streak_start"] is None:
        print("直前の実行は失敗していなかったため、復旧通知はスキップします")
        return

    now = datetime.now(timezone.utc)
    elapsed_hours = (now - streak["streak_start"]).total_seconds() / 3600
    count = streak["count_before_current"]
    try:
        send(topic, _recovered_notification(elapsed_hours, count))
        print(f"更新の復旧を通知しました({elapsed_hours:.1f}時間ぶり、連続失敗{count}回)")
    except (urllib.error.URLError, OSError) as e:
        print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)


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
    args = sys.argv[1:]

    if "--failure" in args:
        topic = os.environ.get("NTFY_TOPIC", "")
        if topic:
            _handle_failure_alert(topic)
        return 0

    if "--recovered" in args:
        topic = os.environ.get("NTFY_TOPIC", "")
        if topic:
            _handle_recovered(topic)
        return 0

    print("usage: notify.py --failure|--recovered", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
