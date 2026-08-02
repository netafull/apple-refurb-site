#!/usr/bin/env python3
"""data/items.json と data/item_state.json から docs/ 一式を生成する。

既存2サイト(電書ポチ・家電ポチ)は「割引率」が軸だが、Apple整備済製品は
新品価格が取得できないため割引率を出せない。代わりに在庫の変化
(新規入荷・値下げ・売り切れ)を軸にする。RSSは在庫一覧ではなく
「出来事のフィード」として組み立てており、購読すれば入荷を追える。
"""

from __future__ import annotations

import datetime
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ITEMS_PATH = ROOT / "data" / "items.json"
STATE_PATH = ROOT / "data" / "item_state.json"
HISTORY_PATH = ROOT / "data" / "count_history.json"
DOCS = ROOT / "docs"
JST = datetime.timezone(datetime.timedelta(hours=9))

CSS = """
:root {
  --bg: #fafaf7; --card: #ffffff; --text: #1a1a1a; --muted: #6b6b6b;
  --accent: #0071e3; --line: #e5e2dc; --hot: #d0342c; --new: #0a7d3c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14151a; --card: #1e2027; --text: #e8e8e6; --muted: #9a9a96;
    --line: #2c2e36; --accent: #2997ff;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: "Hiragino Sans", "Noto Sans JP", sans-serif;
  line-height: 1.6;
}
header { padding: 28px 16px 12px; max-width: 960px; margin: 0 auto; }
header h1 { font-size: 24px; }
header h1 a { color: var(--text); text-decoration: none;
  display: inline-flex; align-items: center; gap: 8px; }
/* ロゴは文字とほぼ同じ高さに揃える */
header h1 img { width: 32px; height: 32px; }
header p { color: var(--muted); font-size: 13px; margin-top: 4px; }
.sites { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;
  align-items: baseline; }
.sites .lbl { font-size: 12px; color: var(--muted); }
.sites a { font-size: 12px; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--card);
  color: var(--text); text-decoration: none; }
.sites a:hover { border-color: var(--accent); color: var(--accent); }
footer .sites { margin-top: 10px; }
main { max-width: 960px; margin: 0 auto; padding: 8px 16px 48px; }
h2 { font-size: 18px; margin: 0; padding-left: 10px;
  border-left: 4px solid var(--accent); display: inline; }
details { margin-top: 28px; }
summary { cursor: pointer; list-style: none; user-select: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: "\\25bc"; font-size: 11px; color: var(--muted);
  margin-right: 8px; }
details:not([open]) summary::before { content: "\\25b6"; }
summary:hover h2 { color: var(--accent); }
details > .grid, details > .empty, details > .cmeta, details > .gone {
  margin-top: 12px; }
.cmeta { color: var(--muted); font-size: 12px; }
/* カテゴリ見出しの前日比。運営者が在庫の動きを追うための表示 */
.delta { font-size: 12px; font-weight: 600; margin-left: 6px; }
.delta.up { color: var(--new); }
.delta.down { color: var(--hot); }
.grid { display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
.item { display: flex; gap: 12px; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; padding: 12px;
  text-decoration: none; color: var(--text); }
/* flexアイテムはmin-width:autoのため、長いタイトルが枠をはみ出す。
   0にして縮小を許可する(既存2サイトで踏んだのと同じ罠) */
.item > div { min-width: 0; }
.item:hover { border-color: var(--accent); }
.item img { width: 76px; height: 76px; object-fit: contain; border-radius: 4px;
  flex-shrink: 0; background: var(--line); }
.item .t { font-size: 14px; font-weight: 600; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.price { margin-top: 6px; font-size: 14px; }
.price .now { font-weight: 700; }
.price .was { font-size: 12px; color: var(--muted);
  text-decoration: line-through; margin-left: 6px; }
.off { display: inline-block; font-size: 11px; font-weight: 700;
  color: #fff; background: var(--hot); border-radius: 4px;
  padding: 1px 6px; margin-left: 6px; vertical-align: 1px; }
.meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
.tag { display: inline-block; font-size: 10px; font-weight: 600;
  color: var(--muted); border: 1px solid var(--line); border-radius: 4px;
  padding: 0 5px; margin-bottom: 3px; }
.tag.new { color: #fff; background: var(--new); border-color: var(--new); }
.gone { font-size: 13px; color: var(--muted); }
.gone li { list-style: none; padding: 3px 0; border-bottom: 1px solid var(--line); }
footer { max-width: 960px; margin: 0 auto; padding: 16px;
  color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); }
.empty { color: var(--muted); font-size: 14px; padding: 12px 0; }
/* サイトの説明。訪問者の目的(セール情報)を邪魔しないよう本文の最後に置く。
   AIは位置に関わらずページ全体を読むため、下でも検索・AI向けの効果は落ちない */
.about { max-width: 960px; margin: 40px auto 0; padding: 20px 16px 0;
  border-top: 1px solid var(--line); color: var(--muted); font-size: 13px;
  line-height: 1.9; }
.about h2 { font-size: 14px; border-left-width: 3px; margin-bottom: 8px;
  color: var(--text); }
.about p { margin-top: 8px; }
"""


def esc(s):
    return html.escape(s or "", quote=True)


def has_asset(name: str) -> bool:
    """画像は他AIで生成してから配置する運用なので、無い間は参照しない。

    存在しないファイルをHTMLから参照すると404が出るため、生成時に確認する。
    """
    return (DOCS / "assets" / name).is_file()


def clean_title(title: str) -> str:
    """「[整備済製品]」を落とす。

    全商品に必ず付くため情報量がゼロなのに10文字近く占め、
    肝心のチップ名(M5 Max等)が3行の折り返しで切れてしまう。
    """
    for token in ("[整備済製品]", "（整備済製品）", "(整備済製品)", "【整備済製品】"):
        title = title.replace(token, "")
    return " ".join(title.split())


def md(iso: str) -> str:
    try:
        d = datetime.date.fromisoformat(iso)
        return f"{d.month}/{d.day}"
    except (TypeError, ValueError):
        return ""


def hours_ago(at: str, date_only: str, now: datetime.datetime,
              today: datetime.date) -> float | None:
    """出来事からの経過時間。時刻が無い既存データは日付から概算する。"""
    if at:
        try:
            return (now - datetime.datetime.fromisoformat(at)).total_seconds() / 3600
        except ValueError:
            pass
    if date_only:
        try:
            return (today - datetime.date.fromisoformat(date_only)).days * 24
        except ValueError:
            pass
    return None


def days_ago(iso: str, today: datetime.date) -> int | None:
    try:
        return (today - datetime.date.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return None


def delta_html(slug: str, now: int, prev: dict) -> str:
    """前日比を見出しに添える。増減が無い日と比較できない日は何も出さない。"""
    if slug not in prev:
        return ""
    diff = now - prev[slug]
    if diff == 0:
        return ""
    cls = "up" if diff > 0 else "down"
    return f'<span class="delta {cls}">{diff:+d}</span>'


def load_prev_counts(today: datetime.date) -> dict:
    """前日のカテゴリ別件数を返す。運営者が在庫の動きを追うための材料。

    GitHub Actionsは発火をスキップすることがあり前日ぶんが無い場合もあるので、
    直近の過去日を遡って探し、見つからなければ空を返す(比較を出さない)。
    """
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    past = sorted(d for d in history if d < today.isoformat())
    if not past:
        return {}
    return (history[past[-1]] or {}).get("categories") or {}


def build_events(
    items: list[dict], state: dict, today: datetime.date,
    now: datetime.datetime,
) -> dict:
    """在庫一覧と履歴を突き合わせて「入荷」「値下げ」「売り切れ」を作る。"""
    new_hours = CONFIG.get("new_arrival_hours", 24)
    drop_days = CONFIG.get("price_drop_days", 14)
    gone_days = CONFIG.get("sold_out_days", 7)

    arrivals, drops = [], []
    for it in items:
        entry = state.get(it["part_number"]) or {}
        history = entry.get("price_history") or []

        # 新着は「初検出が最近」か「再入荷が最近」のいずれか。
        # 再入荷は初検出日が変わらないため、first_seenだけ見ると取り逃がす
        since_ago = hours_ago(
            entry.get("first_seen_at") or "", it.get("since", ""), now, today
        )
        restock_ago = hours_ago(
            entry.get("restocked_at_at") or "", entry.get("restocked_at") or "",
            now, today,
        )
        # 初回実行で一括登録した商品は新着ではない。印が付いている間は
        # first_seenが最近でも新着として扱わない(再入荷は別途拾う)
        is_new = (
            since_ago is not None
            and since_ago <= new_hours
            and not entry.get("baseline")
        )
        is_restock = restock_ago is not None and restock_ago <= new_hours
        if is_new or is_restock:
            row = dict(it)
            # 再入荷と新規入荷が重なった場合は、より新しい出来事を採用する
            row["is_restock"] = is_restock and (
                not is_new or (restock_ago is not None and since_ago is not None and restock_ago <= since_ago)
            )
            row["event_date"] = (
                entry.get("restocked_at") if row["is_restock"] else it.get("since")
            )
            # サイト上では初入荷と再入荷を区別しない。Apple整備済製品は
            # 同じ構成が繰り返し入荷するため、運用が続くほど「過去に見た型番」
            # ばかりになり、いずれ大半が再入荷に該当して両者の意味が逆転する。
            # 買う側の関心は「今あるかどうか」であって履歴上の初出ではない。
            # ただしRSSでは区別を残す(特定モデルを待つ購読者には意味がある)
            arrivals.append(row)

        # 直近の価格変化が値下げなら拾う。値上げは表示しない
        if len(history) >= 2:
            (d_new, p_new), (_, p_old) = history[-1], history[-2]
            gap = days_ago(d_new, today)
            if p_new < p_old and gap is not None and gap <= drop_days:
                row = dict(it)
                row["was"] = p_old
                row["dropped_at"] = d_new
                row["off"] = round((p_old - p_new) / p_old * 100)
                drops.append(row)

    in_stock = {it["part_number"] for it in items}
    gone = []
    for part, entry in state.items():
        if part in in_stock:
            continue
        at = entry.get("sold_out_at")
        gap = days_ago(at or "", today)
        if gap is not None and gap <= gone_days:
            gone.append({**entry, "part_number": part, "sold_out_at": at})

    # 初回実行の在庫は state 側の baseline 印で除外済み。ここでは
    # 「まだ一度も新着が出ていない状態か」だけを判定して案内文の出し分けに使う。
    # 以前は「新着の件数が在庫と一致するか」で初回を推測していたが、
    # 初日の全在庫が7日間ずっと新着候補に残るため、その間ほんとうの新着まで
    # 抑制される不具合があった(実際に7/31のMacBook Air 2件を取り逃がした)
    baseline = bool(items) and all(
        (state.get(it["part_number"]) or {}).get("baseline") for it in items
    )

    arrivals.sort(key=lambda x: (x.get("event_date") or "", x["price"]), reverse=True)
    drops.sort(key=lambda x: x["off"], reverse=True)
    gone.sort(key=lambda x: (x.get("sold_out_at") or "", x.get("price", 0)), reverse=True)
    return {"arrivals": arrivals, "drops": drops, "gone": gone, "baseline": baseline}


def render_item(it: dict, tag: str | None = None) -> str:
    img = (
        f'<img src="{esc(it.get("image"))}" alt="" loading="lazy">'
        if it.get("image")
        else "<img alt=''>"
    )
    tag_html = ""
    if tag == "new":
        tag_html = '<div class="tag new">NEW</div>'
    elif tag:
        tag_html = f'<div class="tag">{esc(tag)}</div>'

    was_html = off_html = ""
    if it.get("was"):
        was_html = f'<span class="was">&yen;{int(it["was"]):,}</span>'
        off_html = f'<span class="off">{it["off"]}%値下げ</span>'

    # Appleの次元値は "12_9inch" のような内部表記なので読める形に直す
    screen = it.get("screen", "").replace("_", ".").replace("inch", "インチ")
    spec = " / ".join(
        v for v in [it.get("capacity", "").upper(), it.get("memory", "").upper(),
                    screen, it.get("year", "")] if v
    )
    meta = []
    stamp = it.get("event_date") or it.get("since")
    if stamp:
        meta.append(f"{md(stamp)}入荷")
    if it.get("shipping"):
        meta.append(it["shipping"])
    meta_html = f'<div class="meta">{esc(" ・ ".join(meta))}</div>' if meta else ""
    spec_html = f'<div class="meta">{esc(spec)}</div>' if spec else ""

    # Appleのアフィリエイトプログラムは終了しており報酬は発生しないので、
    # rel に sponsored は付けない(既存2サイトのAmazonリンクとはここが違う)
    return f"""<a class="item" href="{esc(it["url"])}" target="_blank" rel="noopener">
  {img}
  <div>
    {tag_html}<div class="t">{esc(clean_title(it["title"]))}</div>
    <div class="price"><span class="now">&yen;{int(it["price"]):,}</span>{was_html}{off_html}</div>
    {spec_html}
    {meta_html}
  </div>
</a>"""


def generate_html(data: dict, state: dict, events: dict) -> str:
    generated = datetime.datetime.fromisoformat(data["generated_at"]).astimezone(JST)
    updated = generated.strftime("%Y年%m月%d日 %H:%M")
    today = generated.date()
    items = data["items"]
    site_url = CONFIG.get("site_url", "")

    sections = []

    arrivals = events["arrivals"]
    if arrivals:
        cards = "\n".join(
            render_item(it, tag="new")
            for it in arrivals
        )
        sections.append(
            '<details open id="new">\n'
            f'<summary><h2>🆕 新着 ({len(arrivals)}件)</h2></summary>\n'
            f'<p class="cmeta">直近{CONFIG.get("new_arrival_hours", 24)}時間以内に'
            '在庫に現れた商品です（再入荷を含みます）</p>\n' 
            f'<div class="grid">\n{cards}\n</div>\n</details>'
        )
    elif events.get("baseline"):
        # 初日に「新着0件」とだけ出ると壊れているように見えるため理由を書く
        sections.append(
            '<details open id="new">\n'
            '<summary><h2>🆕 新着</h2></summary>\n'
            '<p class="cmeta">在庫の記録を開始しました。'
            '次回の更新以降、新しく入荷した商品をここに掲載します</p>\n</details>'
        )

    drops = events["drops"]
    if drops:
        cards = "\n".join(render_item(it) for it in drops)
        sections.append(
            '<details open id="drop">\n'
            f'<summary><h2>💰 値下げ ({len(drops)}件)</h2></summary>\n'
            f'<p class="cmeta">直近{CONFIG.get("price_drop_days", 14)}日以内に'
            '価格が下がった商品です</p>\n'
            f'<div class="grid">\n{cards}\n</div>\n</details>'
        )

    # 各カテゴリの一覧にも同じ印を付ける。上部の節を見ていない人にも
    # 新しく入ったものが分かるようにするため、判定は上部と共通にする
    new_parts = {a["part_number"] for a in arrivals}

    # 前日比はカテゴリ見出しにだけ添える。入荷・売り切れの節は増減の内訳
    # そのものなので、そこに差分を出すと二重表現になる
    prev_counts = load_prev_counts(today)

    # カテゴリ別の在庫一覧。config.jsonの並び順を保ち、0件は末尾に回す
    by_cat: dict[str, list[dict]] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    # accessories は Mac/HomePod 等と品揃えが完全に重複するため、
    # 型番で重複排除すると常に0件になる。取得は続ける(将来ここだけに出る
    # 商品があるかもしれない)が、独立したセクションとしては表示しない
    cats = sorted(
        (c for c in CONFIG["categories"] if c.get("display", True)),
        key=lambda c: 0 if by_cat.get(c["slug"]) else 1,
    )
    for i, cat in enumerate(cats):
        # 入荷情報のサイトなので新着を上に。同じ入荷日の中では安い順にする
        # (整備済製品を探す人は価格重視なので、高額モデルが先頭を占めない)。
        # ソートが安定なので、価格昇順→入荷日降順の順に掛ければ両立する
        rows = sorted(by_cat.get(cat["slug"]) or [], key=lambda x: x["price"])
        rows.sort(key=lambda x: x.get("since") or "", reverse=True)
        if rows:
            body = (
                f'<div class="grid">\n'
                + "\n".join(
                    render_item(r, tag="new" if r["part_number"] in new_parts else None)
                    for r in rows
                )
                + "\n</div>"
            )
        else:
            body = '<p class="empty">現在在庫はありません。</p>'
        sections.append(
            f'<details open id="c{i}">\n'
            f'<summary><h2>{esc(cat["name"])} ({len(rows)}件)'
            f'{delta_html(cat["slug"], len(rows), prev_counts)}</h2></summary>\n' 
            f'{body}\n</details>'
        )

    gone = events["gone"]
    if gone:
        # 売り切れた商品ページは辿れなくなるためリンクにしない
        lis = "\n".join(
            f'<li>{md(g.get("sold_out_at") or "")} ・ &yen;{int(g.get("price") or 0):,} ・ {esc(clean_title(g.get("title") or ""))}</li>'
            for g in gone
        )
        sections.append(
            '<details id="gone">\n'
            f'<summary><h2>❌ 最近売り切れ ({len(gone)}件)</h2></summary>\n'
            f'<p class="cmeta">直近{CONFIG.get("sold_out_days", 7)}日以内に'
            '在庫が無くなった商品です。再入荷することがあります</p>\n'
            f'<ul class="gone">\n{lis}\n</ul>\n</details>'
        )

    tagline = CONFIG.get("site_tagline", "")
    page_title = f'{CONFIG["site_title"]}｜{tagline}' if tagline else CONFIG["site_title"]

    # Search Consoleの所有権確認。既存2サイトはGA連携で済んだためタグ不要
    # だったが、その方式が使えない場合に備えて設定できるようにしておく
    gsv = CONFIG.get("google_site_verification", "")
    gsv_tag = (
        f'<meta name="google-site-verification" content="{esc(gsv)}">' if gsv else ""
    )

    # メディアポリシー(プライバシーポリシー・AdSenseのCookie告知を含む)は
    # netaful.jp/policy.html に既にある。3サイトとも netaful.jp 配下なので
    # 各サイトに複製せずリンクで参照する
    policy_url = CONFIG.get("policy_url", "")
    policy_link = (
        f'｜ <a href="{esc(policy_url)}" style="color:inherit">メディアポリシー</a>\n'
        if policy_url
        else ""
    )

    # AdSenseの広告コード。ads.txtはルートドメイン(netaful.jp)のものが
    # サブドメインにも適用されるため、各サイトでの設置は不要
    adsense_id = CONFIG.get("adsense_client_id", "")
    adsense_tag = (
        '<script async src="https://pagead2.googlesyndication.com/pagead/js/'
        f'adsbygoogle.js?client={esc(adsense_id)}" crossorigin="anonymous"></script>'
        if adsense_id
        else ""
    )

    ga_id = CONFIG.get("ga_measurement_id")
    ga_tag = ""
    if ga_id:
        ga_tag = (
            f'<script async src="https://www.googletagmanager.com/gtag/js?id={esc(ga_id)}"></script>\n'
            "<script>window.dataLayer=window.dataLayer||[];"
            "function gtag(){dataLayer.push(arguments);}"
            f"gtag('js',new Date());gtag('config','{esc(ga_id)}');</script>"
        )

    icon_tags = []
    if has_asset("favicon.png"):
        icon_tags.append('<link rel="icon" type="image/png" href="assets/favicon.png">')
    if has_asset("apple-touch-icon.png"):
        icon_tags.append('<link rel="apple-touch-icon" href="assets/apple-touch-icon.png">')
    ogp_tags = []
    if has_asset("ogp.jpg"):
        ogp_tags = [
            f'<meta property="og:image" content="{esc(site_url)}assets/ogp.jpg">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
    logo = (
        '<img src="assets/logo.png" alt="" width="32" height="32">'
        if has_asset("logo.png")
        else ""
    )

    # サイトの説明。データ元・更新頻度・掲載基準・運営者を明記して、
    # 検索エンジンやAIが「このサイトは何者か」を判断できるようにする
    about = CONFIG.get("about") or []
    about_html = ""
    if about:
        paras = "\n".join(f"<p>{esc(x)}</p>" for x in about)
        about_html = (
            f'<section class="about">\n'
            f'<h2>{esc(CONFIG["site_title"])}について</h2>\n{paras}\n</section>'
        )

    related = CONFIG.get("related_sites") or []
    related_html = ""
    if related:
        # 説明文は既存2サイト(電書ポチ・家電ポチ)と同じく可視テキストで出す。
        # title属性だけだとマウスを乗せるまで読めず、モバイルでは全く見えない
        links = "\n".join(
            f'<a href="{esc(s["url"])}">{esc(s["name"])}'
            + (f'<span class="lbl"> {esc(s["desc"])}</span>' if s.get("desc") else "")
            + "</a>"
            for s in related
        )
        related_html = f'<nav class="sites"><span class="lbl">関連サイト</span>\n{links}\n</nav>'

    # 姉妹2サイトと揃えて、扱っているカテゴリの一覧も構造化データに出す。
    # dateModified は毎時更新という強みを機械に伝えるため
    # (画面上の「最終更新」表記は機械には読めない)
    json_ld = json.dumps(
        [
            {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": CONFIG["site_title"],
                "url": site_url,
                "description": CONFIG["site_description"],
                "dateModified": generated.isoformat(timespec="seconds"),
            },
            {
                "@context": "https://schema.org",
                "@type": "ItemList",
                "name": "在庫があるApple整備済製品のカテゴリ",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "name": cat["name"],
                    }
                    for i, cat in enumerate(cats)
                ],
            },
        ],
        ensure_ascii=False,
    ).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(page_title)}</title>
<meta name="description" content="{esc(CONFIG["site_description"])}">
<link rel="canonical" href="{esc(site_url)}">
{gsv_tag}
{ga_tag}
{adsense_tag}
{chr(10).join(icon_tags)}
<meta property="og:type" content="website">
<meta property="og:title" content="{esc(page_title)}">
<meta property="og:description" content="{esc(CONFIG["site_description"])}">
<meta property="og:url" content="{esc(site_url)}">
<meta property="og:site_name" content="{esc(CONFIG["site_title"])}">
<meta property="og:locale" content="ja_JP">
{chr(10).join(ogp_tags)}
<link rel="alternate" type="application/rss+xml" title="RSS" href="rss.xml">
<script type="application/ld+json">{json_ld}</script>
<style>{CSS}</style>
</head>
<body>
<header>
<h1><a href="./">{logo}{esc(CONFIG["site_title"])}</a></h1>
<p>{esc(CONFIG["site_description"])} ｜ 在庫{len(items)}件 ｜ 最終更新: {updated}</p>
{related_html}
</header>
<main>
{chr(10).join(sections)}
{about_html}
</main>
<footer>
価格・在庫は取得時点のものです。整備済製品は在庫が少なく売り切れが早いため、
購入前にApple公式サイトで最新の状況をご確認ください。
当サイトはApple Inc.とは一切関係のない非公式サイトです。
Apple、Mac、iPad、iPhoneなどはApple Inc.の商標です。
{policy_link}｜ <a href="rss.xml" style="color:inherit">RSS</a>
{related_html}
</footer>
</body>
</html>
"""


def generate_rss(data: dict, events: dict) -> str:
    """在庫一覧ではなく「出来事」のフィードにする。

    在庫をそのまま並べると毎回ほぼ同じ内容になり購読の意味がない。
    入荷・再入荷・値下げだけを流し、guidに日付を含めることで
    同じ商品が再入荷したときも新しい記事として届く。
    """
    site_url = CONFIG.get("site_url", "")
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    entries = []
    for it in events["arrivals"]:
        kind = "再入荷" if it.get("is_restock") else "新着入荷"
        entries.append(
            (f"【{kind}】{clean_title(it['title'])} ¥{int(it['price']):,}", it["url"],
             f"{it['part_number']}-{kind}-{it.get('event_date') or ''}",
             it.get("category_name", ""))
        )
    for it in events["drops"]:
        entries.append(
            (f"【{it['off']}%値下げ】{clean_title(it['title'])} ¥{int(it['was']):,} → ¥{int(it['price']):,}",
             it["url"], f"{it['part_number']}-drop-{it['dropped_at']}",
             it.get("category_name", ""))
        )
    # 在庫が大きく入れ替わった回に何百件も流れないよう上限を設ける
    entries = entries[: CONFIG.get("rss_max_items", 100)]
    items_xml = "\n".join(
        f"""<item>
<title>{esc(t)}</title>
<link>{esc(u)}</link>
<guid isPermaLink="false">{esc(g)}</guid>
<category>{esc(c)}</category>
</item>"""
        for t, u, g, c in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>{esc(CONFIG["site_title"])}</title>
<link>{esc(site_url)}</link>
<description>{esc(CONFIG["site_description"])}</description>
<lastBuildDate>{now}</lastBuildDate>
{items_xml}
</channel>
</rss>
"""


def generate_sitemap(data: dict) -> str:
    site_url = CONFIG.get("site_url", "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url>
<loc>{esc(site_url)}</loc>
<lastmod>{data["generated_at"][:10]}</lastmod>
<changefreq>hourly</changefreq>
</url>
</urlset>
"""


def main() -> int:
    data = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    generated = datetime.datetime.fromisoformat(data["generated_at"]).astimezone(JST)
    today = generated.date()
    events = build_events(data["items"], state, today, generated)

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(generate_html(data, state, events), encoding="utf-8")
    (DOCS / "rss.xml").write_text(generate_rss(data, events), encoding="utf-8")
    (DOCS / "sitemap.xml").write_text(generate_sitemap(data), encoding="utf-8")
    (DOCS / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {CONFIG.get('site_url', '')}sitemap.xml\n",
        encoding="utf-8",
    )
    (DOCS / "CNAME").write_text(
        CONFIG.get("site_url", "").replace("https://", "").strip("/") + "\n",
        encoding="utf-8",
    )
    # このサイトのPagesはブランチ(main /docs)直参照で配信しており、
    # そのままではJekyllが走って「_」で始まるファイルが黙って除外される。
    # 姉妹2サイトはアーティファクト方式なのでこの問題は起きない
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(
        f"generated: index.html, rss.xml, sitemap.xml, robots.txt, CNAME, .nojekyll "
        f"(在庫{data['count']}件 / 新着{len(events['arrivals'])}件 "
        f"値下げ{len(events['drops'])}件 売り切れ{len(events['gone'])}件)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())