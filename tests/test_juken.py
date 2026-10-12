"""scripts/juken.py のテスト：偽のニュースと一時ファイルを使うので、ネットにも Discord にもつながない。

    python -m unittest discover tests
"""

import datetime as dt
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import juken  # noqa: E402
import sources  # noqa: E402

NOW = dt.datetime(2026, 12, 9, 3, 0, tzinfo=dt.timezone.utc)


def cfg():
    return {
        "settings": {"timezone": "Asia/Tokyo", "lookback_days": 7, "remind_days": [7, 1, 0],
                     "exclude": ["高校入試"], "countdowns": ["kyotsu"], "name": "美幸",
                     "season_start": "2026-10-01", "season_end": "2027-03-31"},
        "schedule": [
            {"id": "kyotsu", "date": "2027-01-16", "end": "2027-01-17", "title": "共通テスト本試験",
             "short": "共通テスト", "kind": "共通テスト", "important": True},
            {"date": "2026-12-16", "title": "受験票の確認", "kind": "共通テスト", "remind": True},
            {"date": "2026-11-01", "title": "もう終わった", "kind": "手続き", "important": True},
        ],
        "targets": [{"name": "テスト大学", "short": "テ大", "dates": [{"date": "2027-02-25", "title": "個別試験"}]}],
        "topics": [{"name": "共通テスト", "queries": ["共通テスト"], "match": ["共通テスト"], "urgent": ["出願者数"]}],
        "timetable": [], "checklist": [{"group": "出願", "items": ["写真"]}], "links": [], "changes": [],
        "plan": [{"from": "2026-12-01", "to": "2026-12-25", "title": "共テ実戦モード", "todo": ["受験票を印刷"]},
                 {"from": "2026-11-01", "to": "2026-11-30", "title": "穴を埋める", "todo": []}],
    }


def entry(title, url, days_ago=1):
    return sources.Entry(title=title, url=url, published=NOW - dt.timedelta(days=days_ago), source="テスト新聞")


class JukenTest(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        for name, path in (("NEWS_FILE", tmp / "news.json"), ("NOTIFIED_FILE", tmp / "notified.json")):
            p = mock.patch.object(juken, name, path)
            p.start()
            self.addCleanup(p.stop)

    def test_events_merge_targets_and_sort(self):
        evs = juken.events(cfg())
        self.assertEqual([x["start"] for x in evs], sorted(x["start"] for x in evs))
        school = next(x for x in evs if x["school"])
        self.assertEqual(school["title"], "テスト大学　個別試験")
        self.assertEqual(school["short"], "テ大 個別試験")
        self.assertEqual(school["kind"], "志望校")

    def test_collect_filters_and_dedupes(self):
        feed = [
            entry("共通テスト出願者数 49万人", "u1"),
            entry("共通テスト出願者数 49万人（速報）", "u2"),   # 同じ話の転載
            entry("公立高校入試の日程 共通テスト", "u3"),        # exclude
            entry("無関係なニュース", "u4"),                    # match しない
            entry("共通テスト 古い記事", "u5", days_ago=30),     # lookback 外
        ]
        c = cfg()
        c["targets"] = []
        added = juken.collect(c, now=NOW, fetch=lambda q: feed)
        self.assertEqual([x["url"] for x in added], ["u1"])
        self.assertTrue(added[0]["urgent"])
        # 二回目は同じ記事を追加しない
        self.assertEqual(juken.collect(c, now=NOW, fetch=lambda q: feed), [])

    def test_reminders_on_exact_days(self):
        c = cfg()
        evs = juken.events(c)
        due = juken.due_reminders(c, evs, dt.date(2026, 12, 9))     # 受験票 7日前
        self.assertEqual([ev["title"] for _, _, ev in due], ["受験票の確認"])
        due = juken.due_reminders(c, evs, dt.date(2027, 1, 16))     # 本番当日
        self.assertEqual([(n, ev["id"]) for _, n, ev in due], [(0, "kyotsu")])
        # 志望校の日程は remind を書かなければ通知しない
        self.assertEqual(juken.due_reminders(c, evs, dt.date(2027, 2, 24)), [])

    def test_notify_first_run_is_silent_then_sends_once(self):
        c = cfg()
        sent = []
        with mock.patch.object(juken, "_send", lambda p: sent.append(p) or True):
            juken.run_notify(c, now=dt.datetime(2026, 12, 9, 9))
            self.assertEqual(sent, [])                       # 初回は記録だけ
            juken.run_notify(c, now=dt.datetime(2027, 1, 15, 9))
            self.assertEqual(len(sent), 1)                   # 共通テスト前日
            self.assertIn("明日", sent[0]["embeds"][0]["title"])
            juken.run_notify(c, now=dt.datetime(2027, 1, 15, 11))
            self.assertEqual(len(sent), 1)                   # 同じ日にもう一度は送らない

    def test_render_page(self):
        page = juken.render_page(cfg(), dt.date(2026, 12, 9), "2026-12-09 12:00")
        self.assertIn('data-cur="2026-12"', page)              # 今月のカレンダーを最初に表示
        self.assertIn("ガントチャート", page)
        self.assertEqual(page.count('class="panel"'), 4)              # 4つのタブ
        self.assertIn("テ大 個別試験", page)
        self.assertIn('class="cnt"', page)                      # カウントダウン
        self.assertIn(">38<", page)                             # 12/9 → 1/16 は 38 日
        self.assertNotIn("もう終わった</b>", page)              # 終わった予定は「次の予定」に出さない

    def test_plan_marks_current_phase(self):
        page = juken.render_page(cfg(), dt.date(2026, 12, 9), "2026-12-09 12:00")
        self.assertIn("いまやること", page)
        self.assertIn("この期間あと17日", page)                 # 12/9〜12/25
        self.assertIn('class="pl done"', page)                  # 11月の期間は済
        self.assertIn('class="pl cur"', page)
        self.assertIn("nowph", page)                            # ガントチャートでも今の期間を強調
        # どの期間にも入らない日は「いまやること」を出さない
        self.assertNotIn('class="nowbox"', juken.render_page(cfg(), dt.date(2027, 1, 5), ""))

    def test_gantt_positions_inside_season(self):
        html = juken.render_gantt(juken.events(cfg()), dt.date(2026, 12, 9),
                                  dt.date(2026, 10, 1), dt.date(2027, 3, 31))
        self.assertIn("今日", html)
        for left in __import__("re").findall(r"left:([\d.]+)%", html):
            self.assertLessEqual(float(left), 100)


if __name__ == "__main__":
    unittest.main()
