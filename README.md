# Cours publics Kraken (EUR)

Toutes les 10 minutes, GitHub Actions lit l'API publique de Kraken et publie `marche-kraken.json` sur la branche `donnees` :

```
https://raw.githubusercontent.com/StopRun/kraken-marche-eur/donnees/marche-kraken.json
```

Ce fichier alimente une veille automatique qui ne peut pas joindre `api.kraken.com` directement, mais lit `raw.githubusercontent.com`.

## Contenu
- `ticker` : toutes les paires contre euro (hors monnaies et stablecoins) et les paires cotées en BTC ou en ETH ; dernier cours, meilleurs prix, plus haut et plus bas sur 24 h, ouverture du jour, volume en unités et en euros.
- `h1` : 48 bougies horaires de BTC/EUR, ETH/EUR et des paires contre euro d'au moins 1 M€ échangés sur 24 h.
- `d1` : 15 bougies journalières des paires contre euro d'au moins 100 000 € et des paires croisées d'au moins 50 000 €, relues toutes les heures.
- `fng` : indice peur et avidité (alternative.me), relu toutes les heures.
- `univers` : paires contre euro d'au moins 250 000 € sur 24 h triées par volume, et nouvelles cotations des 30 derniers jours.
- `erreurs` : incidents du dernier passage. Chaque bloc porte l'heure `t` de sa dernière lecture réussie ; un bloc en échec garde sa valeur précédente.

Uniquement des données publiques de marché : aucune clé, aucun solde, aucune position. Les paires sont choisies par volume d'échange.

## Fonctionnement
- `relais.py` : bibliothèque standard de Python, sans dépendance. Appels Kraken espacés de 1,1 s.
- `.github/workflows/relais.yml` : essais, relais, puis publication d'un commit unique remplacé à chaque passage.
- Les tâches planifiées de GitHub partent souvent avec quelques minutes de retard, et `raw.githubusercontent.com` peut servir une copie vieille de 5 minutes : les données ont en général entre 0 et 20 minutes.
- GitHub désactive les tâches planifiées d'un dépôt public sans activité pendant 60 jours ; la publication de la branche `donnees` compte normalement comme activité. Si le fichier cesse d'être mis à jour, voir l'onglet Actions.

## Arrêter
Onglet Actions > « Relais des cours Kraken » > « … » > « Disable workflow ».

## Essais
```
python3 -m unittest discover -s tests
```
