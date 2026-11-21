"""docs/feed.json(ポチフル用の新着フィード)のテスト。Appleサイトには出ない。

実行: python3 -m unittest discover -s tests -v
"""

import datetime
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import generate_site  # noqa: E402

JST = generate_site.JST
NOW = datetime.datetime(2026, 11, 20, 21, 0, tzinfo=JST)


def item(part, cat="mac", price=98800, since="2026-11-20"):
    return {
        "part_number": part, "title": f"MacBook {part} [整備済製品]", "price": price,
        "url": f"https://www.apple.com/jp/shop/product/{part}", "image": f"https://img/{part}.jpg",
        "category": cat, "since": since,
    }


class FeedTest(unittest.TestCase):
    def feed(self, items, state):
        return generate_site.generate_feed_data({"items": items}, state, NOW)

    def test_shape(self):
        f = self.feed(
            [item("A")], {"A": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T20:00:00+09:00"}}
        )
        self.assertEqual((f["version"], f["site"]), (1, "apple"))
        it = f["items"][0]
        self.assertEqual(it["id"], "apple:A")
        self.assertEqual(it["title"], "MacBook A")
        self.assertEqual(it["note"], "¥98,800")
        self.assertEqual(it["url"], "https://apple.netaful.jp/mac/")
        self.assertEqual(it["date"], "2026-11-20")
        self.assertNotIn("image_link", it)
        self.assertEqual(it["source"], "apple")

    def test_window_order_baseline_and_restock(self):
        state = {
            "OLD": {"first_seen": "2026-11-16", "first_seen_at": "2026-11-16T10:00:00+09:00"},
            "MID": {"first_seen": "2026-11-18", "first_seen_at": "2026-11-18T23:00:00+09:00"},
            "NEW": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T09:00:00+09:00"},
            "BASE": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T09:00:00+09:00", "baseline": True},
            "RE": {"first_seen": "2026-10-01", "first_seen_at": "2026-10-01T09:00:00+09:00",
                   "restocked_at": "2026-11-19", "restocked_at_at": "2026-11-19T12:00:00+09:00"},
        }
        items = [item("OLD", since="2026-11-16"), item("MID", since="2026-11-18"), item("NEW"),
                 item("BASE"), item("RE", since="2026-10-01")]
        f = self.feed(items, state)
        self.assertEqual([i["id"] for i in f["items"]], ["apple:NEW", "apple:RE", "apple:MID"])

    def test_note_shows_recent_drop(self):
        # 入荷かつ直近に値下げ(84,800→71,800)あり → 旧価格→現価格。値下げなしは価格だけ
        state = {
            "D": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T09:00:00+09:00",
                  "price_history": [["2026-11-18", 84800], ["2026-11-20", 71800]]},
            "N": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T09:00:00+09:00",
                  "price_history": [["2026-11-20", 98800]]},
            "UP": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T09:00:00+09:00",
                   "price_history": [["2026-11-18", 60000], ["2026-11-20", 98800]]},
        }
        f = self.feed([item("D", price=71800), item("N"), item("UP")], state)
        notes = {i["id"]: i["note"] for i in f["items"]}
        self.assertEqual(notes["apple:D"], "¥84,800→¥71,800（15%OFF）")
        self.assertEqual(notes["apple:N"], "¥98,800")
        self.assertEqual(notes["apple:UP"], "¥98,800")  # 値上げは出さない

    def test_drop_without_arrival_adds_no_item(self):
        # 入荷でない(古い)商品の値下げは、feed に新しい項目を増やさない
        state = {"OLD": {"first_seen": "2026-10-01", "first_seen_at": "2026-10-01T09:00:00+09:00",
                         "price_history": [["2026-11-18", 84800], ["2026-11-20", 71800]]}}
        f = self.feed([item("OLD", price=71800, since="2026-10-01")], state)
        self.assertEqual(f["items"], [])

    def test_max_20(self):
        state = {f"P{n}": {"first_seen": "2026-11-20", "first_seen_at": "2026-11-20T10:00:00+09:00"} for n in range(30)}
        f = self.feed([item(f"P{n}") for n in range(30)], state)
        self.assertEqual(len(f["items"]), 20)


if __name__ == "__main__":
    unittest.main()
