#!/usr/bin/env python3
"""Relais des cours publics de Kraken pour la veille Sentinelle.

Les tâches planifiées de Claude ne peuvent pas joindre api.kraken.com, mais elles lisent
raw.githubusercontent.com. Lancé par GitHub Actions toutes les 10 minutes, ce script écrit
marche-kraken.json, que le workflow publie sur la branche `donnees`.

Uniquement des données publiques de marché : aucune clé, aucun solde, aucune liste de
positions. Les paires relayées sont choisies par volume d'échange, pas par portefeuille.
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

KRAKEN_API = "https://api.kraken.com/0/public/"
FNG_URL = "https://api.alternative.me/fng/?limit=2"
EUR = "ZEUR"
# Paires croisées relayées : cotation en BTC ou en ETH, valorisées par la paire euro de la cotation.
CROSS_QUOTE_EUR_PAIR = {"XXBT": "XBTEUR", "XETH": "ETHEUR"}
# Monnaies et stablecoins : pas des actifs de swing.
FIAT_OR_STABLE = {
    "ZEUR", "ZUSD", "ZGBP", "ZCHF", "ZCAD", "ZAUD", "ZJPY", "USDT", "USDC", "DAI", "PYUSD", "RLUSD",
    "TUSD", "FDUSD", "USDE", "USDS", "USDG", "USDQ", "USD1", "EURT", "EURC", "EUROC", "EURR", "EURQ",
}
# Le régime D et le déclencheur T2 se lisent sur les bougies horaires de BTC et d'ETH :
# toujours relayées, quel que soit leur volume du jour.
INTRADAY_ALWAYS = ("XBTEUR", "ETHEUR")
# Seuils bas : le marché euro de Kraken est mince (moins de 30 paires au-dessus de 250 000 € par jour le 23/11/2026),
# et un actif tenu en lots de 300 à 500 € doit garder ses bougies même un jour calme.
H1_MIN_VOLUME_EUR = 50_000
D1_MIN_VOLUME_EUR = 50_000
CROSS_MIN_VOLUME_EUR = 25_000
# Assez liquide pour un lot de 300 à 500 € : base de la découverte de candidats.
UNIVERSE_MIN_VOLUME_EUR = 250_000
# 30 h : de quoi lire 1 h, 4 h et 24 h glissantes.
H1_KEEP = 30
D1_KEEP = 15
# Un peu moins d'une heure, pour qu'un passage sur six (toutes les 10 min) relise les blocs lents.
SLOW_REFRESH_S = 55 * 60
# Une paire déjà vue n'est jamais « nouvelle » : une suspension suivie d'une reprise ne compte pas.
NEW_LISTING_KEEP_S = 30 * 24 * 3600
# API publique Kraken : environ un appel par seconde avant limitation.
KRAKEN_PAUSE_S = 1.1
HTTP_TIMEOUT_S = 20
NOTE = (
    "Chaque bloc porte l'heure t de sa dernière lecture réussie ; un bloc en échec garde sa valeur précédente. "
    "Ticker, par nom Kraken (ex. XBTEUR) : c dernier, b bid, a ask, h24/l24 plus haut/bas 24 h glissantes, "
    "o ouverture du jour UTC, v24 volume 24 h en unités, vol_eur volume 24 h en euros. "
    "Bougies : [t epoch s, o, h, l, c, volume], la dernière de chaque série est en cours ; une série en échec "
    "garde ses bougies précédentes, juger sa fraîcheur sur la dernière. "
    f"h1 : XBTEUR, ETHEUR et paires euro d'au moins {H1_MIN_VOLUME_EUR} € sur 24 h. "
    f"d1 : paires euro d'au moins {D1_MIN_VOLUME_EUR} € et croisées BTC/ETH d'au moins {CROSS_MIN_VOLUME_EUR} €. "
    f"Univers : liquides = [nom, c, o, h24, l24, vol_eur] des paires euro d'au moins {UNIVERSE_MIN_VOLUME_EUR} €, "
    "triées par volume ; nouvelles = paires euro apparues depuis 30 jours."
)


class KrakenError(Exception):
    pass


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "kraken-marche-eur/1"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        return json.load(response)


class Kraken:
    def __init__(self, fetch, clock, sleep):
        self.fetch = fetch
        self.clock = clock
        self.sleep = sleep
        self.last_call = None

    def call(self, method, query=""):
        if self.last_call is not None:
            wait = self.last_call + KRAKEN_PAUSE_S - self.clock()
            if wait > 0:
                self.sleep(wait)
        self.last_call = self.clock()
        body = self.fetch(KRAKEN_API + method + ("?" + query if query else ""))
        if body.get("error"):
            raise KrakenError(method + " : " + "; ".join(body["error"]))
        return body["result"]


def is_relayed(info):
    """Paire euro d'un actif qui n'est ni une monnaie ni un stablecoin, ou paire croisée BTC/ETH."""
    if info.get("status", "online") != "online" or info["base"] in FIAT_OR_STABLE:
        return False
    return info["quote"] == EUR or info["quote"] in CROSS_QUOTE_EUR_PAIR


def read_pairs(kraken):
    """{nom Kraken: [clé canonique, nom affiché, cotation]} des paires relayées."""
    result = kraken.call("AssetPairs")
    return {
        info["altname"]: [key, info.get("wsname", info["altname"]), info["quote"]]
        for key, info in sorted(result.items())
        if is_relayed(info)
    }


def read_ticker(kraken, pairs):
    result = kraken.call("Ticker")
    rows = {}
    for alt, (key, _, quote) in pairs.items():
        row = result.get(key)
        if row:
            rows[alt] = {"c": row["c"][0], "b": row["b"][0], "a": row["a"][0], "h24": row["h"][1],
                         "l24": row["l"][1], "o": row["o"], "v24": row["v"][1], "_quote": quote,
                         "_vwap": row["p"][1]}
    for row in rows.values():
        quote = row.pop("_quote")
        vwap = float(row.pop("_vwap"))
        if quote == EUR:
            quote_eur = 1.0
        else:
            quote_row = rows.get(CROSS_QUOTE_EUR_PAIR[quote])
            quote_eur = float(quote_row["c"]) if quote_row else None
        row["vol_eur"] = round(float(row["v24"]) * vwap * quote_eur) if quote_eur else None
    return rows


def select(ticker, pairs, minimum_eur, minimum_cross_eur=None, always=()):
    chosen = [alt for alt in always if alt in ticker]
    for alt, row in sorted(ticker.items(), key=lambda item: -(item[1]["vol_eur"] or 0)):
        is_cross = pairs[alt][2] != EUR
        limit = minimum_cross_eur if is_cross else minimum_eur
        if limit is not None and (row["vol_eur"] or 0) >= limit and alt not in chosen:
            chosen.append(alt)
    return chosen


def read_candles(kraken, pairs, chosen, minutes, keep, now_s, previous, errors, block):
    """Une série par paire ; une paire en échec garde sa série précédente et l'erreur est notée."""
    since = int(now_s) - (keep + 1) * minutes * 60
    series = {}
    for alt in chosen:
        try:
            result = kraken.call("OHLC", f"pair={alt}&interval={minutes}&since={since}")
            rows = result.get(pairs[alt][0]) or next(v for k, v in result.items() if k != "last")
            series[alt] = [[row[0], row[1], row[2], row[3], row[4], row[6]] for row in rows[-keep:]]
        except Exception as error:  # noqa: BLE001 — une paire ne doit pas faire tomber les autres
            errors.append({"bloc": block, "texte": f"{alt} : {error}"})
            if alt in previous:
                series[alt] = previous[alt]
    if chosen and not any(alt in series for alt in chosen):
        raise KrakenError(f"aucune série {block} lue")
    return series


def read_fear_greed(fetch):
    return [{"valeur": row["value"], "classe": row["value_classification"], "t": iso(int(row["timestamp"]))}
            for row in fetch(FNG_URL)["data"]]


def universe(ticker, pairs, previous, now_s):
    eur = sorted(alt for alt in ticker if pairs[alt][2] == EUR)
    known = previous.get("connues") if previous else None
    fresh = [{"cle": alt, "vue": iso(now_s)} for alt in eur if known is not None and alt not in known]
    kept = [item for item in (previous or {}).get("nouvelles", [])
            if now_s - datetime.strptime(item["vue"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            < NEW_LISTING_KEEP_S]
    liquid = sorted(([alt, ticker[alt]["c"], ticker[alt]["o"], ticker[alt]["h24"], ticker[alt]["l24"],
                      ticker[alt]["vol_eur"]] for alt in eur if (ticker[alt]["vol_eur"] or 0) >= UNIVERSE_MIN_VOLUME_EUR),
                    key=lambda row: -row[5])
    return {"liquides": liquid, "nouvelles": kept + fresh,
            "connues": sorted(set(known) | set(eur)) if known is not None else eur}


def age_s(block, now_s):
    if not block or "t" not in block:
        return None
    return now_s - datetime.strptime(block["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def relay(previous, fetch=fetch_json, clock=time.time, sleep=time.sleep):
    kraken = Kraken(fetch, clock, sleep)
    errors = []

    def due(name):
        age = age_s(previous.get(name), clock())
        return age is None or age >= SLOW_REFRESH_S

    def refresh(name, is_due, reader):
        if not is_due:
            return previous.get(name)
        try:
            return {"t": iso(clock()), "data": reader()}
        except Exception as error:  # noqa: BLE001 — le bloc garde sa valeur et son heure d'origine
            errors.append({"bloc": name, "texte": str(error)})
            return previous.get(name)

    pairs_block = refresh("paires", due("paires"), lambda: read_pairs(kraken))
    pairs = pairs_block["data"] if pairs_block else {}
    ticker_block = refresh("ticker", bool(pairs), lambda: read_ticker(kraken, pairs))
    # refresh rend l'objet précédent lui-même en cas d'échec : l'identité dit si le ticker est frais.
    ticker_fresh = ticker_block is not None and ticker_block is not previous.get("ticker")
    ticker = ticker_block["data"] if ticker_block else {}
    # Les sélections ne gardent que les paires connues de la table courante.
    ticker = {alt: row for alt, row in ticker.items() if alt in pairs}

    h1_previous = (previous.get("h1") or {}).get("data", {})
    h1 = refresh("h1", bool(ticker), lambda: read_candles(
        kraken, pairs, select(ticker, pairs, H1_MIN_VOLUME_EUR, always=INTRADAY_ALWAYS),
        60, H1_KEEP, clock(), h1_previous, errors, "h1"))
    d1_previous = (previous.get("d1") or {}).get("data", {})
    d1 = refresh("d1", bool(ticker) and due("d1"), lambda: read_candles(
        kraken, pairs, select(ticker, pairs, D1_MIN_VOLUME_EUR, CROSS_MIN_VOLUME_EUR),
        1440, D1_KEEP, clock(), d1_previous, errors, "d1"))
    fng = refresh("fng", due("fng"), lambda: read_fear_greed(fetch))
    universe_previous = (previous.get("univers") or {}).get("data")
    universe_block = refresh("univers", ticker_fresh, lambda: universe(ticker, pairs, universe_previous, clock()))

    return {
        "v": 1,
        "maj": iso(clock()),
        "source": "API publique Kraken et alternative.me, relayées par GitHub Actions toutes les 10 min",
        "note": NOTE,
        "paires": pairs_block,
        "ticker": ticker_block,
        "h1": h1,
        "d1": d1,
        "fng": fng,
        "univers": universe_block,
        "erreurs": errors,
    }


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--precedent", default="precedent.json")
    parser.add_argument("--sortie", default="marche-kraken.json")
    args = parser.parse_args(argv)
    payload = relay(load_previous(args.precedent))
    with open(args.sortie, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
    count = len(payload["ticker"]["data"]) if payload["ticker"] else 0
    print(f"{args.sortie} écrit à {payload['maj']} : {count} paires au ticker, {len(payload['erreurs'])} erreur(s)")
    for error in payload["erreurs"]:
        print(f"  {error['bloc']} : {error['texte']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
