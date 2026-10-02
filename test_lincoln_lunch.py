import contextlib
import datetime as dt
import io
import unittest
import urllib.error

import lincoln_lunch as L


def item(day, name, category=None, hidden=None):
    return {"day": day, "month": None, "year": None, "hidden": hidden, "product": {"name": name, "category": category}}


SPACER = (None, None)


def menu(items):
    # The API's month is 0-based: 9 is October.
    return L.parse_menu({"id": "abc", "year": 2026, "month": 9, "items": items,
                         "previousMonthPublished": {"id": "prev"}, "nextMonthPublished": None})


class MenuDaysTest(unittest.TestCase):
    def day(self, *entries):
        return L.menu_days(menu([item(1, *e) for e in entries]))[0]

    def test_parse_menu(self):
        m = menu([])
        self.assertEqual((m.year, m.month, m.prev_id, m.next_id), (2026, 10, "prev", None))

    def test_spacers_separate_dishes(self):
        d = self.day(("Colonel Crunch Chicken Sandwich", "Entrees"), ("w/ French Fries",), SPACER,
                     ("Plant Based ChiK'n Patty", "Entrees"), ("w/ French Fries",), SPACER,
                     ("Salad Bar Featured Item: Fiesta Corn Salad", "Sides"))
        self.assertEqual(d.date, dt.date(2026, 10, 1))
        self.assertEqual([str(x) for x in d.dishes], [
            "Colonel Crunch Chicken Sandwich w/ French Fries",
            "Plant Based ChiK'n Patty w/ French Fries",
            "Salad Bar Featured Item: Fiesta Corn Salad",
        ])
        self.assertEqual(d.summary, "Colonel Crunch Chicken Sandwich / Plant Based ChiK'n Patty")

    def test_sides_join_with_comma(self):
        d = self.day(("Mandarin Orange Chicken", "Entrees"), ("Oven Vegetable Fried Rice", "Sides"),
                     ("& Fortune Cookie", "Sides"))
        self.assertEqual(str(d.dishes[0]), "Mandarin Orange Chicken, Oven Vegetable Fried Rice & Fortune Cookie")

    def test_accompaniment_filed_as_entree(self):
        d = self.day(("Pancakes", "Entrees"), ("w/ Breakfast Turkey Sausage", "Entrees"), SPACER,
                     ("Grass-Fed Beef Hot Dog", "Entrees"), ("in whole wheat bun",))
        self.assertEqual([str(x) for x in d.dishes],
                         ["Pancakes w/ Breakfast Turkey Sausage", "Grass-Fed Beef Hot Dog in whole wheat bun"])

    def test_missing_spacers(self):
        d = self.day(("Pepperoni Pizza", "Entrees"), ("Cheese Pizza", "Entrees"),
                     ("Oven Vegetable Fried Rice", "Sides"),
                     ("Salad Bar Featured Item: Mighty Caesar Salad", "Sides"))
        self.assertEqual([str(x) for x in d.dishes], [
            "Pepperoni Pizza",
            "Cheese Pizza, Oven Vegetable Fried Rice",
            "Salad Bar Featured Item: Mighty Caesar Salad",
        ])

    def test_whitespace_hidden_and_invalid_days(self):
        days = L.menu_days(menu([
            item(2, "Macaroni & Cheese", "Entrees"), item(2, "w/  aloha  bread"),
            item(2, "Secret", "Entrees", hidden=True),
            item(3, None), item(32, "Ghost Pizza", "Entrees"),
        ]))
        self.assertEqual([(d.date.day, [str(x) for x in d.dishes]) for d in days],
                         [(2, ["Macaroni & Cheese w/ aloha bread"])])

    def test_summary_without_entrees(self):
        d = self.day(("Turkey Sandwich", ""), SPACER, ("Hummus Plate", None))
        self.assertEqual(d.summary, "Turkey Sandwich / Hummus Plate")


class RetryTest(unittest.TestCase):
    def run_with(self, codes):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) <= len(codes):
                raise urllib.error.HTTPError("https://x", codes[len(calls) - 1], "err", {}, None)
            return "ok"

        with contextlib.redirect_stderr(io.StringIO()):
            return L.with_retries(fn, delay=0), len(calls)

    def test_retries_cloudflare_and_server_errors(self):
        self.assertEqual(self.run_with([403, 429, 502]), ("ok", 4))

    def test_does_not_retry_client_errors(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.run_with([404])

    def test_gives_up(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.run_with([503] * 5)


class IcsTest(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(L.ics_escape("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")

    def test_fold(self):
        line = "DESCRIPTION:" + "é" * 100
        folded = L.ics_fold(line)
        physical = folded.split("\r\n")
        self.assertTrue(all(len(p.encode()) <= 75 for p in physical))
        self.assertEqual("".join(p[1:] if i else p for i, p in enumerate(physical)), line)

    def test_fold_keeps_escapes_together(self):
        for prefix in range(60, 80):
            line = "DESCRIPTION:" + "x" * prefix + "\\n" * 40 + "\\\\" * 40
            physical = L.ics_fold(line).split("\r\n")
            for p in physical[:-1]:
                trailing = len(p) - len(p.rstrip("\\"))
                self.assertEqual(trailing % 2, 0, p)
            self.assertEqual("".join(p[1:] if i else p for i, p in enumerate(physical)), line)

    def test_build_ics(self):
        d = L.Day(date=dt.date(2026, 10, 30), menu_id="abc",
                  dishes=[L.Dish(["Pepperoni Pizza"], "entrees"), L.Dish(["Cheese Pizza"], "entrees")])
        ics = L.build_ics([d], dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        unfolded = ics.replace("\r\n ", "")
        for line in ["UID:20261030@lincoln-lunch.ianloic.github.io", "DTSTART;VALUE=DATE:20261030",
                     "DTEND;VALUE=DATE:20261031", "SUMMARY:Pepperoni Pizza / Cheese Pizza",
                     "DTSTAMP:20261002T120000Z"]:
            self.assertIn(line + "\r\n", unfolded)


if __name__ == "__main__":
    unittest.main()
