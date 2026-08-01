#!/usr/bin/env python3
"""Apple公式の整備済製品ストアから在庫一覧を取得し、入荷・値下げ・売り切れを検知する。

Apple整備済製品ストアの各カテゴリページには window.REFURB_GRID_BOOTSTRAP という
JSONがそのまま埋め込まれており、HTMLの構造解析なしに商品情報を取り出せる。
Amazonと違いこのページは検索結果ではなく在庫の完全なリストなので、
「消えた = 売り切れ」と断定できる点が既存2サイト(電書ポチ・家電ポチ)と大きく違う。
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ITEMS_PATH = ROOT / "data" / "items.json"
STATE_PATH = ROOT / "data" / "item_state.json"
# カテゴリ別の件数を日次で残す。前日比の表示に使う
HISTORY_PATH = ROOT / "data" / "count_history.json"

JST = datetime.timezone(datetime.timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# 1カテゴリ1リクエストなので全体でも7リクエスト。それでも間隔は空ける
REQUEST_INTERVAL = 1.0


def fetch_html(slug: str, base_url: str) -> str:
    url = f"{base_url}/jp/shop/refurbished/{slug}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", "replace")


def extract_bootstrap(html: str) -> dict | None:
    """window.REFURB_GRID_BOOTSTRAP のJSONを括弧の対応を数えて切り出す。

    正規表現で終端を探すとJSON内の括弧に引っかかるため、文字列リテラルと
    エスケープを考慮しながら深さを数える。
    """
    m = re.search(r"window\.REFURB_GRID_BOOTSTRAP\s*=\s*", html)
    if not m:
        return None
    i = m.end()
    while i < len(html) and html[i] not in "{[":
        i += 1
    if i >= len(html):
        return None
    begin = i
    depth = 0
    in_str = False
    escaped = False
    while i < len(html):
        c = html[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        return json.loads(html[begin : i + 1])
    except json.JSONDecodeError:
        return None


def parse_price(tile: dict) -> int | None:
    raw = ((tile.get("price") or {}).get("currentPrice") or {}).get("raw_amount")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def normalize(tile: dict, category: dict, base_url: str) -> dict | None:
    part = tile.get("partNumber")
    title = tile.get("title")
    if not part or not title:
        return None  # 商品以外のタイルが混ざっていても落ちないように
    price = parse_price(tile)
    if price is None:
        return None

    # productDetailsUrl は fnode= という絞り込みトークンと、日本語を
    # パーセントエンコードした長いスラッグを含む。1URLで300バイト超になり
    # 生成HTMLの大半をURLが占めてしまう。型番だけの短縮形でも
    # 正規URLへ200でリダイレクトされることを確認済みなのでそちらを使う
    # 型番 "FHFA4J/A" が URL の ".../product/fhfa4j/a" に対応する
    url = f"{base_url}/jp/shop/product/{part.lower()}"

    image = ""
    sources = ((tile.get("image") or {}).get("sources")) or []
    if sources:
        image = sources[0].get("srcSet") or ""

    dims = ((tile.get("filters") or {}).get("dimensions")) or {}
    return {
        "part_number": part,
        "title": title,
        "price": price,
        "url": url,
        "image": image,
        "category": category["slug"],
        "category_name": category["name"],
        "shipping": (tile.get("omnitureModel") or {}).get("customerCommitString") or "",
        "model": dims.get("refurbClearModel") or "",
        "year": dims.get("dimensionRelYear") or "",
        "capacity": dims.get("dimensionCapacity") or "",
        "screen": dims.get("dimensionScreensize") or "",
        "color": dims.get("dimensionColor") or "",
        "memory": dims.get("tsMemorySize") or "",
    }


def load_state() -> dict:
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    # 既存2サイトでコンフリクトマーカーが混入したままコミットされた事故が
    # 実際に起きたため、CI運用に移す前提で最初から検出しておく
    if "<<<<<<<" in raw or ">>>>>>>" in raw:
        print(
            f"[warn] {STATE_PATH.name} にコンフリクトマーカーが混入しています。"
            "履歴が失われる恐れがあるため中止します",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[warn] {STATE_PATH.name} が壊れています ({e})。中止します", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base_url = config.get("base_url", "https://www.apple.com")
    categories = config["categories"]

    items: dict[str, dict] = {}
    ok_slugs: set[str] = set()
    per_category: list[tuple[str, int]] = []

    for i, cat in enumerate(categories):
        if i:
            time.sleep(REQUEST_INTERVAL)
        try:
            html = fetch_html(cat["slug"], base_url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"[warn] {cat['slug']}: 取得失敗 ({e})", file=sys.stderr)
            continue
        data = extract_bootstrap(html)
        if data is None:
            # Appleが埋め込み構造を変えた場合ここに来る。
            # 全カテゴリで起きたら最後にまとめて異常終了させる
            print(f"[warn] {cat['slug']}: REFURB_GRID_BOOTSTRAP が見つかりません", file=sys.stderr)
            continue

        ok_slugs.add(cat["slug"])
        count = 0
        for tile in data.get("tiles") or []:
            item = normalize(tile, cat, base_url)
            if item is None:
                continue
            count += 1
            # accessories は homepod 等と品揃えが重複する。先に出た方を採用する
            items.setdefault(item["part_number"], item)
        per_category.append((cat["name"], count))

    if not items:
        # 空の結果で状態ファイルを上書きすると全履歴が消えるため必ず失敗させる
        print("[error] 1件も取得できませんでした。状態ファイルは更新しません", file=sys.stderr)
        return 1

    today_dt = datetime.datetime.now(JST).date()
    today = today_dt.isoformat()
    state = load_state()
    first_run = not state

    new_arrivals: list[dict] = []
    restocked: list[dict] = []
    price_drops: list[tuple[dict, int]] = []
    price_rises: list[tuple[dict, int]] = []
    new_state: dict[str, dict] = {}

    for part, item in items.items():
        prev = state.get(part)
        if prev is None:
            entry = {
                "first_seen": today,
                "last_seen": today,
                "title": item["title"],
                "category": item["category"],
                "price": item["price"],
                # 価格履歴は後から遡って作れないので初回から必ず残す。
                # 変化があったときだけ追記するので件数は増えにくい
                "price_history": [[today, item["price"]]],
                "sold_out_at": None,
                # 初回実行で一括登録した商品には印を付ける。これが無いと
                # サイト側は「新着の件数が在庫と一致するか」で初回を推測する
                # しかなく、初日の全在庫がnew_arrival_days(7日)の間ずっと
                # 新着候補に残るため、その間ほんとうの新着まで抑制されてしまう
                "baseline": True if first_run else None,
            }
            if not first_run:
                new_arrivals.append(item)
        else:
            entry = dict(prev)
            entry["last_seen"] = today
            entry["title"] = item["title"]
            # カテゴリは初回に決めたものを保つ。accessories は Mac/HomePod などと
            # 品揃えが重複するため、一方の取得が失敗した回に上書きすると
            # 商品の所属カテゴリが実行ごとに揺れてしまう
            entry.setdefault("category", item["category"])
            if prev.get("sold_out_at"):
                restocked.append(item)
                entry["sold_out_at"] = None
                # 再入荷は初検出日(first_seen)が変わらないため、これを
                # 記録しないとサイト側で新着として扱えない
                entry["restocked_at"] = today
            old_price = prev.get("price")
            if isinstance(old_price, int) and old_price != item["price"]:
                history = list(prev.get("price_history") or [])
                history.append([today, item["price"]])
                entry["price_history"] = history
                if item["price"] < old_price:
                    price_drops.append((item, old_price))
                else:
                    price_rises.append((item, old_price))
            entry["price"] = item["price"]
            entry.setdefault("price_history", [[today, item["price"]]])
        item["since"] = entry["first_seen"]
        new_state[part] = entry

    # 売り切れ判定は取得に成功したカテゴリに限る。取得失敗したカテゴリの商品を
    # 売り切れ扱いにすると、一時的な通信エラーが誤検知として全件に波及する
    sold_out: list[dict] = []
    retention = config.get("state_retention_days", 365)
    dropped = 0
    for part, entry in state.items():
        if part in new_state:
            continue
        entry = dict(entry)
        if entry.get("category") in ok_slugs and not entry.get("sold_out_at"):
            entry["sold_out_at"] = today
            sold_out.append(entry)
        gone = entry.get("sold_out_at")
        if gone:
            try:
                elapsed = (today_dt - datetime.date.fromisoformat(gone)).days
            except (TypeError, ValueError):
                elapsed = 0
            # 売り切れ後も一定期間は残す。再入荷の検知と価格履歴のために必要
            if elapsed > retention:
                dropped += 1
                continue
        new_state[part] = entry

    ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
                "count": len(items),
                "items": sorted(items.values(), key=lambda x: (x["category"], -x["price"])),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    STATE_PATH.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # 前日比を出すためにカテゴリ別の件数を日次で記録する。
    # 毎時上書きするので、その日の最後の実行値が残る。
    # 1日1行しか増えないためファイルは小さいままで済む
    history = {}
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        print(f"[warn] {HISTORY_PATH.name} が壊れています ({e})。作り直します", file=sys.stderr)
    counts: dict[str, int] = {}
    for item in items.values():
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    history[today] = {"total": len(items), "categories": counts}
    # 保持期間を超えた分は捨てる(state と同じ基準にそろえる)
    cutoff = (today_dt - datetime.timedelta(days=retention)).isoformat()
    history = {d: v for d, v in history.items() if d >= cutoff}
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )

    # ---- レポート ----
    stamp = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    print(f"=== Apple整備済製品 {stamp} JST ===")
    print(f"在庫 {len(items)}件  " + " / ".join(f"{n} {c}" for n, c in per_category))
    if len(ok_slugs) < len(categories):
        print(f"[注意] {len(categories) - len(ok_slugs)}カテゴリの取得に失敗しています")

    if first_run:
        print(f"\n初回実行のためベースラインを記録しました({len(items)}件)。")
        print("次回以降の実行で入荷・値下げ・売り切れを検知します。")
        return 0

    def show(label: str, rows: list[dict]) -> None:
        if not rows:
            return
        print(f"\n{label} {len(rows)}件")
        for it in sorted(rows, key=lambda x: -x.get("price", 0)):
            print(f"  [{it.get('category_name') or it.get('category')}] ¥{it['price']:,}  {it['title']}")
            if it.get("url"):
                print(f"      {it['url']}")

    show("🆕 新規入荷", new_arrivals)
    show("🔁 再入荷", restocked)

    if price_drops:
        print(f"\n💰 値下げ {len(price_drops)}件")
        for it, old in sorted(price_drops, key=lambda x: (x[1] - x[0]["price"]) / x[1], reverse=True):
            rate = (old - it["price"]) / old * 100
            print(f"  [{it['category_name']}] ¥{old:,} → ¥{it['price']:,} (-{rate:.1f}%)  {it['title']}")
            print(f"      {it['url']}")
    if price_rises:
        print(f"\n📈 値上げ {len(price_rises)}件")
        for it, old in price_rises:
            print(f"  [{it['category_name']}] ¥{old:,} → ¥{it['price']:,}  {it['title']}")
    if sold_out:
        print(f"\n❌ 売り切れ {len(sold_out)}件")
        for e in sold_out:
            print(f"  [{e.get('category')}] ¥{e.get('price', 0):,}  {e.get('title')}")

    if not any([new_arrivals, restocked, price_drops, price_rises, sold_out]):
        print("\n変化はありませんでした。")
    if dropped:
        print(f"\n({dropped}件を保持期間{retention}日超過で削除)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
