"""Essais du relais avec des réponses Kraken simulées au format de l'API publique."""
import json
import os
import re
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relais  # noqa: E402

START = 1795437600  # 2026-11-23T12:40:00Z

# altname: (clé canonique, base, cotation, dernier cours, volume 24 h en unités, statut)
PAIRS = {
    "XBTEUR": ("XXBTZEUR", "XXBT", "ZEUR", "56000", "100", "online"),
    "ETHEUR": ("XETHZEUR", "XETH", "ZEUR", "1580", "2000", "online"),
    "ALPHAEUR": ("ALPHAEUR", "ALPHA", "ZEUR", "66", "20000", "online"),
    "BETAEUR": ("BETAEUR", "BETA", "ZEUR", "0.1", "1500000", "online"),
    "TINYEUR": ("TINYEUR", "TINY", "ZEUR", "0.01", "1000", "online"),
    "USDTEUR": ("USDTEUR", "USDT", "ZEUR", "0.95", "9000000", "online"),
    "OFFEUR": ("OFFEUR", "OFF", "ZEUR", "1", "9000000", "cancel_only"),
    "ALPHAXBT": ("ALPHAXBT", "ALPHA", "XXBT", "0.0012", "50000", "online"),
    "ETHXBT": ("XETHXXBT", "XETH", "XXBT", "0.028", "10", "online"),
    "XBTUSD": ("XXBTZUSD", "XXBT", "ZUSD", "60000", "100", "online"),
}


class FakeKraken:
    def __init__(self):
        self.now = float(START)
        self.calls = []
        self.fail = {}
        self.extra = {}

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def fetch(self, url):
        self.calls.append((url, self.now))
        self.now += 0.05
        parsed = urlparse(url)
        if parsed.netloc == "api.alternative.me":
            if "fng" in self.fail:
                raise OSError("HTTP 503")
            return {"data": [{"value": "23", "value_classification": "Extreme Fear", "timestamp": "1795392000"}]}
        method = parsed.path.rsplit("/", 1)[-1]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if method in self.fail:
            failure = self.fail[method]
            if isinstance(failure, set):
                if query.get("pair") in failure:
                    return {"error": ["EGeneral:Internal error"]}
            else:
                return {"error": [failure]}
        pairs = dict(PAIRS, **self.extra)
        if method == "AssetPairs":
            return {"error": [], "result": {
                key: {"altname": alt, "wsname": alt[:-3] + "/" + alt[-3:], "base": base, "quote": quote, "status": status}
                for alt, (key, base, quote, _, _, status) in pairs.items()}}
        if method == "Ticker":
            return {"error": [], "result": {
                key: {"a": [px, "1", "1"], "b": [px, "1", "1"], "c": [px, "0.1"], "v": ["1", vol], "p": [px, px],
                      "t": [1, 1], "l": [px, "1"], "h": [px, "9"], "o": px}
                for alt, (key, _, _, px, vol, _) in pairs.items()}}
        if method == "OHLC":
            key = pairs[query["pair"]][0]
            step = int(query["interval"]) * 60
            first = (int(query["since"]) // step + 1) * step
            rows = [[t, "1", "2", "0.5", "1.5", "1.2", "3", 7] for t in range(first, int(self.now) + 1, step)]
            return {"error": [], "result": {key: rows, "last": rows[-1][0]}}
        raise AssertionError("méthode inattendue " + method)


def run(fake, previous=None):
    payload = relais.relay(previous or {}, fetch=fake.fetch, clock=fake.clock, sleep=fake.sleep)
    return json.loads(json.dumps(payload))


def methods(fake):
    out = []
    for url, _ in fake.calls:
        parsed = urlparse(url)
        if parsed.netloc == "api.alternative.me":
            out.append("fng")
            continue
        query = parse_qs(parsed.query)
        name = parsed.path.rsplit("/", 1)[-1]
        if "pair" in query:
            name += ":" + query["pair"][0] + ":" + query["interval"][0]
        out.append(name)
    return out


class RelayTest(unittest.TestCase):
    def test_first_run_selects_by_volume_and_excludes_fiat_stable_offline(self):
        fake = FakeKraken()
        p = run(fake)
        self.assertEqual(p["erreurs"], [])
        self.assertEqual(sorted(p["paires"]["data"]),
                         ["ALPHAEUR", "ALPHAXBT", "BETAEUR", "ETHEUR", "ETHXBT", "TINYEUR", "XBTEUR"])
        self.assertEqual(p["paires"]["data"]["XBTEUR"], ["XXBTZEUR", "XBT/EUR", "ZEUR"])
        self.assertEqual(p["ticker"]["data"]["XBTEUR"],
                         {"c": "56000", "b": "56000", "a": "56000", "h24": "9", "l24": "1", "o": "56000",
                          "v24": "100", "vol_eur": 5600000})
        # Croisée : volume valorisé par le cours euro de la cotation (50 000 × 0,0012 × 56 000 €).
        self.assertEqual(p["ticker"]["data"]["ALPHAXBT"]["vol_eur"], 3360000)
        # BETA : 1 500 000 × 0,10 € = 150 000 € sur 24 h, au-dessus du seuil horaire ; TINY (10 €) en dessous.
        self.assertEqual(list(p["h1"]["data"]), ["XBTEUR", "ETHEUR", "ALPHAEUR", "BETAEUR"])
        self.assertEqual(len(p["h1"]["data"]["XBTEUR"]), relais.H1_KEEP)
        self.assertEqual(p["h1"]["data"]["XBTEUR"][0][5], "3", "volume = colonne 7 de Kraken")
        self.assertEqual(sorted(p["d1"]["data"]), ["ALPHAEUR", "ALPHAXBT", "BETAEUR", "ETHEUR", "XBTEUR"])
        self.assertEqual(len(p["d1"]["data"]["BETAEUR"]), 15)
        self.assertEqual(p["fng"]["data"][0], {"valeur": "23", "classe": "Extreme Fear", "t": "2026-11-23T00:00:00Z"})
        self.assertEqual([row[0] for row in p["univers"]["data"]["liquides"]], ["XBTEUR", "ETHEUR", "ALPHAEUR"])
        self.assertEqual(p["univers"]["data"]["nouvelles"], [], "premier passage : rien de nouveau par construction")
        self.assertIn("TINYEUR", p["univers"]["data"]["connues"])

    def test_kraken_calls_are_spaced(self):
        fake = FakeKraken()
        run(fake)
        times = [t for url, t in fake.calls if "api.kraken.com" in url]
        self.assertGreater(len(times), 8)
        for before, after in zip(times, times[1:]):
            self.assertGreaterEqual(after - before, relais.KRAKEN_PAUSE_S - 1e-6)

    def test_next_run_reads_only_ticker_and_hourly_candles(self):
        fake = FakeKraken()
        first = run(fake)
        fake.now += 600
        fake.calls.clear()
        second = run(fake, first)
        self.assertEqual(methods(fake), ["Ticker", "OHLC:XBTEUR:60", "OHLC:ETHEUR:60", "OHLC:ALPHAEUR:60",
                                         "OHLC:BETAEUR:60"])
        self.assertEqual(second["d1"], first["d1"])
        self.assertEqual(second["paires"], first["paires"])
        self.assertNotEqual(second["ticker"]["t"], first["ticker"]["t"])

    def test_slow_blocks_refresh_after_55_minutes(self):
        fake = FakeKraken()
        first = run(fake)
        fake.now += 55 * 60
        fake.calls.clear()
        run(fake, first)
        called = methods(fake)
        self.assertIn("AssetPairs", called)
        self.assertIn("OHLC:ALPHAXBT:1440", called)
        self.assertIn("fng", called)

    def test_ticker_failure_keeps_previous_block_and_time(self):
        fake = FakeKraken()
        first = run(fake)
        fake.now += 600
        fake.fail["Ticker"] = "EService:Unavailable"
        second = run(fake, first)
        self.assertEqual(second["ticker"], first["ticker"])
        self.assertEqual(second["univers"], first["univers"], "univers non recalculé sur un ticker périmé")
        self.assertIn({"bloc": "ticker", "texte": "Ticker : EService:Unavailable"}, second["erreurs"])
        fake.now += 600
        del fake.fail["Ticker"]
        third = run(fake, second)
        self.assertEqual(third["erreurs"], [])
        self.assertNotEqual(third["ticker"]["t"], first["ticker"]["t"])

    def test_one_failing_series_keeps_its_previous_candles(self):
        fake = FakeKraken()
        first = run(fake)
        fake.now += 600
        fake.fail["OHLC"] = {"ALPHAEUR"}
        fake.calls.clear()
        second = run(fake, first)
        self.assertEqual(second["h1"]["data"]["ALPHAEUR"], first["h1"]["data"]["ALPHAEUR"])
        self.assertIn("OHLC:XBTEUR:60", methods(fake), "les autres séries sont relues")
        self.assertEqual(second["erreurs"], [{"bloc": "h1", "texte": "ALPHAEUR : OHLC : EGeneral:Internal error"}])

    def test_kraken_unreachable_on_first_run_still_produces_a_file(self):
        fake = FakeKraken()
        fake.fail["AssetPairs"] = "EGeneral:Too many requests"
        p = run(fake)
        for block in ("paires", "ticker", "h1", "d1", "univers"):
            self.assertIsNone(p[block], block)
        self.assertIsNotNone(p["fng"])
        self.assertEqual(p["erreurs"], [{"bloc": "paires", "texte": "AssetPairs : EGeneral:Too many requests"}])

    def test_new_listing_reported_once_and_kept_30_days(self):
        fake = FakeKraken()
        first = run(fake)
        fake.now += 55 * 60
        fake.extra = {"NEWEUR": ("NEWEUR", "NEW", "ZEUR", "2", "200000", "online")}
        second = run(fake, first)
        self.assertEqual([n["cle"] for n in second["univers"]["data"]["nouvelles"]], ["NEWEUR"])
        seen = second["univers"]["data"]["nouvelles"][0]["vue"]
        fake.now += 600
        third = run(fake, second)
        self.assertEqual(third["univers"]["data"]["nouvelles"], [{"cle": "NEWEUR", "vue": seen}])
        fake.now += 31 * 24 * 3600
        late = run(fake, third)
        self.assertEqual(late["univers"]["data"]["nouvelles"], [])
        self.assertIn("NEWEUR", late["univers"]["data"]["connues"])

    def test_unreadable_previous_file_starts_fresh(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
            file.write("{tronqué")
        try:
            self.assertEqual(relais.load_previous(file.name), {})
        finally:
            os.unlink(file.name)
        self.assertEqual(relais.load_previous("/inexistant/precedent.json"), {})

    def test_no_portfolio_data_in_the_script(self):
        """Le choix des paires ne doit rien dire du portefeuille : seules BTC et ETH sont nommées."""
        with open(relais.__file__, encoding="utf-8") as file:
            named = set(re.findall(r"\"([A-Z0-9]{2,})(?:EUR|XBT|ETH)\"", file.read()))
        self.assertLessEqual(named, {"XBT", "ETH"})

if __name__ == "__main__":
    unittest.main()
