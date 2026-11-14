"""製品ライン別ページ(固定URL)と出来事の台帳のテスト。

実行: python3 -m unittest discover -s tests
ネットワークには出ない。台帳の読み書きは一時ディレクトリで差し替えて行い、
実データ(data/)には触らない。
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import generate_site  # noqa: E402


def entry(**kw):
    base = {
        "first_seen": "2026-08-01",
        "last_seen": "2026-08-10",
        "title": "MacBook Air [整備済製品] - スターライト",
        "category": "mac",
        "price": 120000,
        "price_history": [["2026-08-01", 120000]],
        "sold_out_at": None,
        "baseline": None,
    }
    base.update(kw)
    return base


class DeriveEventsTest(unittest.TestCase):
    def test_kinds_and_baseline_rule(self):
        state = {
            # 通常の初入荷
            "A": entry(),
            # 記録開始日に一括登録された商品。初入荷は出来事にしない
            "B": entry(first_seen="2026-07-30", baseline=True),
            # baseline でも再入荷は出来事にする
            "C": entry(first_seen="2026-07-30", baseline=True,
                       restocked_at="2026-09-01"),
            # 売り切れ
            "D": entry(sold_out_at="2026-08-05"),
        }
        events = generate_site.derive_events_from_state(state)
        keys = {(e["part_number"], e["kind"]) for e in events}
        self.assertIn(("A", "arrival"), keys)
        self.assertNotIn(("B", "arrival"), keys)
        self.assertNotIn(("B", "restock"), keys)
        self.assertIn(("C", "restock"), keys)
        self.assertNotIn(("C", "arrival"), keys)
        self.assertIn(("D", "sold_out"), keys)

    def test_price_at_uses_history_in_effect_on_that_day(self):
        e = entry(price=90000, price_history=[
            ["2026-08-01", 120000], ["2026-08-20", 100000], ["2026-09-10", 90000],
        ])
        self.assertEqual(generate_site.price_at(e, "2026-08-01"), 120000)
        self.assertEqual(generate_site.price_at(e, "2026-08-19"), 120000)
        self.assertEqual(generate_site.price_at(e, "2026-08-20"), 100000)
        self.assertEqual(generate_site.price_at(e, "2026-12-31"), 90000)
        # 履歴が無い場合は現在価格で代用する
        self.assertEqual(generate_site.price_at({"price": 5000}, "2026-08-01"), 5000)

    def test_merge_dedupes_and_is_idempotent(self):
        state = {"A": entry(), "D": entry(sold_out_at="2026-08-05")}
        derived = generate_site.derive_events_from_state(state)
        once = generate_site.merge_event_history([], derived)
        twice = generate_site.merge_event_history(once, derived)
        # Aの初入荷 + Dの初入荷・売り切れ
        self.assertEqual(len(once), 3)
        self.assertEqual(once, twice)

    def test_event_history_roundtrip(self):
        import tempfile

        state = {"A": entry()}
        events = generate_site.merge_event_history(
            [], generate_site.derive_events_from_state(state)
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "event_history.json"
            original = generate_site.EVENT_HISTORY_PATH
            generate_site.EVENT_HISTORY_PATH = path
            try:
                # 無い場合は空
                self.assertEqual(generate_site.load_event_history(), [])
                generate_site.save_event_history(events)
                self.assertEqual(generate_site.load_event_history(), events)
            finally:
                generate_site.EVENT_HISTORY_PATH = original


class LineCategoriesTest(unittest.TestCase):
    def test_display_categories_only(self):
        slugs = [c["slug"] for c in generate_site.line_categories()]
        # config.json の表示対象と一致し、重複排除専用の accessories は含まない
        self.assertEqual(
            slugs, ["mac", "ipad", "iphone", "watch", "appletv", "homepod"]
        )


def sample_data():
    items = [
        {
            "part_number": "A", "title": "MacBook Air [整備済製品] - スターライト",
            "price": 120000, "url": "https://www.apple.com/jp/shop/product/a",
            "image": "", "category": "mac", "category_name": "Mac",
            "shipping": "", "model": "", "year": "", "capacity": "",
            "screen": "", "color": "", "memory": "", "since": "2026-08-01",
        },
        {
            "part_number": "E", "title": "iPad [整備済製品] - ブルー",
            "price": 50000, "url": "https://www.apple.com/jp/shop/product/e",
            "image": "", "category": "ipad", "category_name": "iPad",
            "shipping": "", "model": "", "year": "", "capacity": "",
            "screen": "", "color": "", "memory": "", "since": "2026-08-02",
        },
    ]
    return {
        "generated_at": "2026-08-10T12:00:00+09:00",
        "count": len(items),
        "items": items,
    }


class CategoryPageTest(unittest.TestCase):
    def page(self):
        data = sample_data()
        state = {
            "A": entry(),
            "D": entry(sold_out_at="2026-08-05",
                       title="Mac mini [整備済製品] - シルバー"),
            "E": entry(category="ipad", title="iPad [整備済製品] - ブルー",
                       price=50000),
        }
        generated = generate_site.datetime.datetime.fromisoformat(
            data["generated_at"]
        ).astimezone(generate_site.JST)
        events = generate_site.build_events(
            data["items"], state, generated.date(), generated
        )
        log = generate_site.merge_event_history(
            [], generate_site.derive_events_from_state(state)
        )
        count_history = {
            "2026-08-09": {"total": 2, "categories": {"mac": 1, "ipad": 1}},
            "2026-08-10": {"total": 2, "categories": {"mac": 1, "ipad": 1}},
        }
        cat = {"slug": "mac", "name": "Mac"}
        return generate_site.generate_category_html(
            cat, data, state, events, log, count_history
        )

    def test_current_line_pill_is_highlighted(self):
        out = self.page()
        nav = re.search(
            r'<nav class="line-links"[^>]*>(.*?)</nav>', out, re.S
        )
        self.assertIsNotNone(nav)
        # 自分(Mac)のピルだけが aria-current でハイライトされる
        # (style内のセレクタ文字列と区別するため nav の中だけを数える)
        self.assertEqual(nav.group(1).count('aria-current="page"'), 1)
        self.assertIn(
            '<a href="../mac/" data-google-vignette="false" '
            'aria-current="page">Mac<span class="n">',
            nav.group(1),
        )
        # 他ラインのピルには付かない
        self.assertIn(
            '<a href="../ipad/" data-google-vignette="false">iPad'
            '<span class="n">',
            nav.group(1),
        )

    def test_canonical_and_title(self):
        out = self.page()
        self.assertIn(
            '<link rel="canonical" href="https://apple.netaful.jp/mac/">', out
        )
        self.assertIn("Mac整備済製品の在庫と入荷履歴", out)

    def test_stock_contains_only_own_category(self):
        out = self.page()
        self.assertIn("MacBook Air", out)
        # 他カテゴリの商品名(在庫カード・履歴)は混ざらない
        self.assertNotIn("iPad [整備済製品] - ブルー", out)

    def test_history_and_counts_and_official_link(self):
        out = self.page()
        # 入荷履歴に売り切れた商品の入荷も残る
        self.assertIn("Mac mini", out)
        self.assertIn("入荷履歴", out)
        # 在庫数の推移(日次)
        self.assertIn("2026-08-09", out)
        # Apple公式のカテゴリページへのリンク
        self.assertIn("https://www.apple.com/jp/shop/refurbished/mac", out)
        # 1階層深いぶんの相対リンク
        self.assertIn('href="../rss.xml"', out)
        self.assertIn('href="../"', out)


class TopAndSitemapTest(unittest.TestCase):
    def real_page(self):
        data = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8"))
        try:
            state = json.loads(
                (ROOT / "data" / "item_state.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            state = {}
        generated = generate_site.datetime.datetime.fromisoformat(
            data["generated_at"]
        ).astimezone(generate_site.JST)
        events = generate_site.build_events(
            data["items"], state, generated.date(), generated
        )
        return data, generate_site.generate_html(data, state, events)

    def test_top_links_to_line_pages(self):
        _, html_text = self.real_page()
        for cat in generate_site.line_categories():
            self.assertIn(f'href="{cat["slug"]}/"', html_text)

    def test_top_has_no_filter_tabs(self):
        _, html_text = self.real_page()
        # 絞り込みタブは廃止済み(ラインページと同じ軸のため)。
        # タブのボタンも .tabs ナビも出ない
        self.assertNotIn("data-target", html_text)
        self.assertNotIn('class="tabs"', html_text)

    def test_top_line_nav_is_pills_with_counts(self):
        _, html_text = self.real_page()
        nav = re.search(
            r'<nav class="line-links"[^>]*>(.*?)</nav>', html_text, re.S
        )
        self.assertIsNotNone(nav)
        # ピルは config の定義順に6ラインぶん並ぶ
        links = re.findall(r'<a href="([a-z]+)/"', nav.group(1))
        self.assertEqual(
            links, [c["slug"] for c in generate_site.line_categories()]
        )
        # 各ピルに件数が付く
        self.assertEqual(nav.group(1).count('<span class="n">'), 6)
        self.assertIn("件</span>", nav.group(1))
        # トップではどのピルもハイライトしない
        self.assertNotIn("aria-current", nav.group(1))

    def test_top_ads_reload_on_details_toggle(self):
        _, html_text = self.real_page()
        # タブ廃止後も、閉じたセクションを開いたときに広告を流し込む
        self.assertIn("function syncAds()", html_text)
        self.assertIn('addEventListener("toggle"', html_text)

    def test_sitemap_includes_line_pages(self):
        data, _ = self.real_page()
        sitemap = generate_site.generate_sitemap(data)
        self.assertIn("<loc>https://apple.netaful.jp/</loc>", sitemap)
        for cat in generate_site.line_categories():
            self.assertIn(
                f'<loc>https://apple.netaful.jp/{cat["slug"]}/</loc>', sitemap
            )


if __name__ == "__main__":
    unittest.main()
