#!/usr/bin/env python3
"""新規入荷をネタフル(WordPress)の下書きとして自動投稿する。

fetch_refurb.pyが書き出したdata/new_arrivals.jsonを読み、対象カテゴリ
(mac/ipad/iphone/watch)のうち新規入荷があったものだけ、そのカテゴリの
現在の全在庫を一覧にした下書き記事を作成する。過去にコグレが手動で
netaful.jpへ投稿してきたもの(WordPress REST APIで実例を確認済み)と
同じ体裁に合わせている。

投稿はXML-RPC(wp.newPost)を使う。REST APIのBasic認証はコアサーバーが
Authorizationヘッダーを剥ぎ取るため通らず(rest_not_logged_in)、実際に
投稿できているMarsEditと同じXML-RPC経由に合わせた。アプリケーション
パスワードはXML-RPCでも通常のパスワードと同様に使える。

公開は必ず手動で行う。このスクリプトは下書き(post_status=draft)を作る
だけで、自動公開はしない。WordPress側の障害・認証エラーなどでも在庫取得
やサイト生成そのものは止めたくないため、失敗時も終了コード0で抜ける
(ワークフロー側でこのステップより前のコミット・pushはすでに終わっている
想定)。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.parsers.expat
import xmlrpc.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ITEMS_PATH = ROOT / "data" / "items.json"
NEW_ARRIVALS_PATH = ROOT / "data" / "new_arrivals.json"

JST = datetime.timezone(datetime.timedelta(hours=9))

# 過去の手動投稿の実績がある4カテゴリのみを対象にする。AppleTV・HomePod・
# アクセサリは前例が無く、タグの用意もまだのため対象外(2026-08-19時点)
TARGET_CATEGORIES = ("mac", "ipad", "iphone", "watch")

INTRO_TEMPLATE = (
    '<img src="{image_url}" alt="{alt}" title="{alt}" border="0" '
    'width="{width}" height="{height}" />\n'
    '<p>Apple公式サイトの「<a target="_blank" href="{apple_link}" '
    'rel="noopener"><strong>{label}整備済製品</strong></a>」の情報です。</p>\n'
    "<p>Appleの整備済み品は問題があって返品された商品などを整備し、"
    "テスト後認定されたものです。1年間の特別保証が付いています。</p>\n"
    '<p>最新の在庫情報は「<a href="{site_url}">林檎ポチ｜Apple整備済製品の'
    "入荷情報</a>」もご利用ください。</p>\n"
    "<p>在庫限りですので、欲しいモデルがあればお早めに！</p>\n"
    "<p><!--more--></p>\n"
    '<h2>「{label}整備済製品」情報</h2>'
)

FOOTER_TEMPLATE = (
    '<p>▼<a href="{site_url}">林檎ポチ｜Apple整備済製品の入荷情報</a></p>\n'
    '<p>▼<a href="{amazon_link}" rel="noopener">Amazonの{label}整備済品の'
    "一覧</a></p>\n"
    '<p>▼<a href="{rakuten_link}" rel="noopener">Apple整備済製品の購入による'
    "楽天ポイント還元情報</a></p>"
)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---- カテゴリごとの機種ファミリー抽出 ----
# タイトルの「【○○整備済製品】MacBook Neo・MacBook Air・…【日付】」の
# 中間部分を、その時点の在庫から動的に組み立てる。固定文字列ではない
# (2026-08-08版はMac Studioを含み2026-08-13版は含まない、など在庫依存)

MAC_FAMILY_ORDER = [
    "MacBook Neo",
    "MacBook Air",
    "MacBook Pro",
    "iMac",
    "Mac Studio",
    "Mac mini",
    "ディスプレイ",
]


def _mac_family(title: str) -> str | None:
    if "MacBook Neo" in title:
        return "MacBook Neo"
    if "MacBook Air" in title:
        return "MacBook Air"
    if "MacBook Pro" in title:
        return "MacBook Pro"
    if "Mac Studio" in title:
        return "Mac Studio"
    if "Mac mini" in title:
        return "Mac mini"
    if "iMac" in title:
        return "iMac"
    if "Studio Display" in title or "Display XDR" in title:
        return "ディスプレイ"
    return None


def mac_families(titles: list[str]) -> list[str]:
    found = {f for f in (_mac_family(t) for t in titles) if f}
    return [f for f in MAC_FAMILY_ORDER if f in found]


# インチ表記を持つもの(「11インチiPad Pro」)と持たないもの(「iPad mini 6」
# 「iPad mini（A17 Pro）」「iPad（A16）」)が混在する。インチを必須にすると
# mini と無印iPadを丸ごと取り逃がし、その2つしか在庫が無い日はタイトルが
# 「【iPad整備済製品】iPad【...】」と機種名なしになる。
# 商品名は必ずタイトル先頭に来るのでmatchで当て、同じカテゴリに混ざる
# アクセサリ(Apple Pencil等)を巻き込まないようにする
_IPAD_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)インチ)?iPad(?:\s*(Pro|Air|mini))?")
# 無印 → mini → Air → Pro の順(下位モデルから並べる)
_IPAD_SUBFAMILY_ORDER = {"": 0, "mini": 1, "Air": 2, "Pro": 3}


def ipad_families(titles: list[str]) -> list[str]:
    seen: dict[str, tuple[int, float]] = {}
    for t in titles:
        m = _IPAD_RE.match(t)
        if not m:
            continue
        inch, sub = m.group(1), m.group(2) or ""
        label = (f"{inch}インチiPad" if inch else "iPad") + (f" {sub}" if sub else "")
        seen[label] = (
            _IPAD_SUBFAMILY_ORDER.get(sub, 9), float(inch) if inch else 0.0
        )
    return [label for label, _ in sorted(seen.items(), key=lambda kv: kv[1])]


# \d+e? だけだと「16e」の"e"が数字の外に取り残されて「iPhone 16」に
# 化ける(末尾のeはグループ外なので握りつぶされる)。eも含めて1つの
# 型番として捉える。Airは数字を持たないため別扱いにする
_IPHONE_RE = re.compile(r"iPhone\s+(Air|\d+e?)(?:\s+(Plus|Pro Max|Pro))?")
_IPHONE_SUFFIX_ORDER = {"": 0, "Plus": 1, "Pro": 2, "Pro Max": 3}


def iphone_families(titles: list[str]) -> list[str]:
    seen: dict[str, tuple[int, int, int, int]] = {}
    for t in titles:
        m = _IPHONE_RE.search(t)
        if not m:
            continue
        base, suf = m.group(1), m.group(2) or ""
        label = f"iPhone {base}" + (f" {suf}" if suf else "")
        if base == "Air":
            key = (1, 0, 0, _IPHONE_SUFFIX_ORDER.get(suf, 9))
        else:
            is_e = base.endswith("e")
            num = int(base[:-1]) if is_e else int(base)
            key = (0, num, 1 if is_e else 0, _IPHONE_SUFFIX_ORDER.get(suf, 9))
        seen[label] = key
    return [label for label, _ in sorted(seen.items(), key=lambda kv: kv[1])]


_WATCH_RE = re.compile(r"Apple Watch\s+(Series\s+\d+|SE\s*\d*|Ultra\s+\d+)")
_WATCH_TYPE_ORDER = {"Series": 0, "SE": 1, "Ultra": 2}


def watch_families(titles: list[str]) -> list[str]:
    seen: dict[str, tuple[int, int]] = {}
    for t in titles:
        m = _WATCH_RE.search(t)
        if not m:
            continue
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        kind, _, rest = raw.partition(" ")
        num = int(rest) if rest.isdigit() else 0
        seen[raw] = (_WATCH_TYPE_ORDER.get(kind, 9), num)
    labels = [label for label, _ in sorted(seen.items(), key=lambda kv: kv[1])]
    if not labels:
        return []
    # 「Apple Watch」は先頭の1回だけ付ける(過去投稿の体裁に合わせる)
    return [f"Apple Watch {labels[0]}", *labels[1:]]


FAMILY_EXTRACTORS = {
    "mac": mac_families,
    "ipad": ipad_families,
    "iphone": iphone_families,
    "watch": watch_families,
}


def build_title(label: str, families: list[str], today: datetime.date) -> str:
    joined = "・".join(families) if families else label
    return f"【{label}整備済製品】{joined}【{today.year}年{today.month}月{today.day}日】"


def build_content(cat_cfg: dict, wp_cfg: dict, site_url: str, items: list[dict]) -> str:
    intro = INTRO_TEMPLATE.format(
        image_url=cat_cfg["image_url"],
        alt=cat_cfg["label"],
        width=cat_cfg["image_width"],
        height=cat_cfg["image_height"],
        apple_link=cat_cfg["apple_link"],
        label=cat_cfg["label"],
        site_url=site_url,
    )
    rows = sorted(items, key=lambda it: it["price"])
    body = "\n".join(f"<p>{esc(it['title'])}\n{it['price']:,}円</p>" for it in rows)
    footer = FOOTER_TEMPLATE.format(
        site_url=site_url,
        amazon_link=cat_cfg["amazon_link"],
        label=cat_cfg["label"],
        rakuten_link=wp_cfg["rakuten_link"],
    )
    return f"{intro}\n{body}\n{footer}"


# WordPressの応答を待つ上限(秒)
WORDPRESS_TIMEOUT = 60


def make_transport(url: str) -> xmlrpc.client.Transport:
    """タイムアウト付きのTransportを作る。

    ServerProxy は既定で socket のグローバルタイムアウト(None)を使うため、
    サーバーがTCPは受け付けるのに応答を返さない状態になると wp.newPost が
    永久に返らず、ワークフローがジョブ上限(360分)まで走り続けてしまう。
    ステップの continue-on-error はハングには効かない(失敗ではないため)。
    林檎ポチは30分間隔・cancel-in-progress: false なので、その間ずっと
    後続の実行がキューに詰まってサイトが更新されなくなる
    """
    base = (
        xmlrpc.client.SafeTransport
        if url.lower().startswith("https:")
        else xmlrpc.client.Transport
    )

    class TimeoutTransport(base):  # type: ignore[misc,valid-type]
        def make_connection(self, host):
            conn = super().make_connection(host)
            conn.timeout = WORDPRESS_TIMEOUT
            return conn

    return TimeoutTransport()


def create_draft(wp_cfg: dict, app_password: str, title: str, content: str, tag_id: int) -> int:
    url = wp_cfg["site_url"].rstrip("/") + "/xmlrpc.php"
    server = xmlrpc.client.ServerProxy(url, transport=make_transport(url))
    content_struct = {
        "post_type": "post",
        "post_status": "draft",
        "post_title": title,
        "post_content": content,
        # 名前ではなくIDで指定する(タグ名の表記揺れ・重複作成を避けるため)
        "terms": {
            "category": [wp_cfg["category_id"]],
            "post_tag": [tag_id],
        },
    }
    # blog_idはマルチサイトでなければ実質無視される。単一サイトの慣例で0を渡す
    post_id = server.wp.newPost(0, wp_cfg["username"], app_password, content_struct)
    return int(post_id)


def notify_ntfy(topic: str, title: str, message: str, click_url: str = "") -> None:
    """下書き作成をntfy(https://ntfy.sh/)でプッシュ通知する。

    トピック名はGitHub Secretsで管理し、リポジトリには書かない(公開
    リポジトリなので、トピック名が漏れると誰でも通知を送りつけられる)。
    通知の失敗は本筋に影響させたくないので例外は投げず警告のみ出す。
    ヘッダーだと日本語のエンコードで面倒が起きるため、JSON配信APIを使う
    """
    if not topic:
        return
    payload: dict[str, str] = {"topic": topic, "title": title, "message": message}
    if click_url:
        payload["click"] = click_url
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except (urllib.error.URLError, OSError) as e:
        print(f"[warn] ntfy通知に失敗しました: {e}", file=sys.stderr)


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    wp_cfg = config.get("wordpress")
    if not wp_cfg:
        print("[info] config.jsonにwordpress設定が無いためスキップします")
        return 0

    app_password = os.environ.get("WORDPRESS_APP_PASSWORD")
    if not app_password and not dry_run:
        print(
            "[warn] 環境変数WORDPRESS_APP_PASSWORDが未設定のためスキップします",
            file=sys.stderr,
        )
        return 0

    # 見逃した入荷を後から手動で埋めるための抜け道。fetch_refurb.pyの
    # 検出結果を待たず、指定カテゴリを強制的に「入荷あり」として扱う
    # (workflow_dispatchのforce_category入力からのみ渡される想定)
    force_category = os.environ.get("FORCE_CATEGORY", "").strip()

    try:
        new_arrivals = json.loads(NEW_ARRIVALS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        new_arrivals = []
    except json.JSONDecodeError as e:
        print(f"[warn] {NEW_ARRIVALS_PATH.name} が壊れています ({e})", file=sys.stderr)
        return 0

    arrived = {it["category"] for it in new_arrivals} & set(TARGET_CATEGORIES)
    if force_category in TARGET_CATEGORIES:
        arrived.add(force_category)
        print(f"[info] {force_category}を強制的に対象にします(FORCE_CATEGORY)")
    if not arrived:
        print("対象カテゴリ(mac/ipad/iphone/watch)の新規入荷は無いため投稿はスキップします")
        return 0

    all_items = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))["items"]
    by_cat: dict[str, list[dict]] = {}
    for it in all_items:
        by_cat.setdefault(it["category"], []).append(it)

    today = datetime.datetime.now(JST).date()
    site_url = config.get("site_url", "")
    wp_site_url = wp_cfg.get("site_url", "").rstrip("/")
    ntfy_topic = os.environ.get("NTFY_TOPIC", "")

    for slug in TARGET_CATEGORIES:
        if slug not in arrived:
            continue
        cat_cfg = wp_cfg["categories"].get(slug)
        items = by_cat.get(slug) or []
        if not cat_cfg or not items:
            continue

        families = FAMILY_EXTRACTORS[slug]([it["title"] for it in items])
        title = build_title(cat_cfg["label"], families, today)
        content = build_content(cat_cfg, wp_cfg, site_url, items)

        if dry_run:
            print(f"=== [{slug}] {title} ===")
            print(content)
            print()
            continue

        try:
            post_id = create_draft(wp_cfg, app_password, title, content, cat_cfg["tag_id"])
        except xmlrpc.client.Fault as e:
            print(
                f"[error] {slug}: WordPress投稿に失敗しました "
                f"(fault {e.faultCode}: {e.faultString})",
                file=sys.stderr,
            )
            continue
        except (xmlrpc.client.ProtocolError, OSError) as e:
            print(f"[error] {slug}: WordPress投稿に失敗しました: {e}", file=sys.stderr)
            continue
        except xml.parsers.expat.ExpatError as e:
            # WordPress側がXMLとして壊れた応答を返すことがある(サーバー側の
            # 一時的な不調やPHPの警告混入とみられる)。他カテゴリの投稿を
            # 道連れにしないよう、ここで打ち切らず次のカテゴリへ進む
            print(
                f"[error] {slug}: WordPressの応答が不正なXMLでした: {e}",
                file=sys.stderr,
            )
            continue

        print(f"下書き作成: 「{title}」(post id {post_id})")
        edit_url = f"{wp_site_url}/wp-admin/post.php?post={post_id}&action=edit"
        notify_ntfy(ntfy_topic, "林檎ポチ: 新着下書き", title, click_url=edit_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
