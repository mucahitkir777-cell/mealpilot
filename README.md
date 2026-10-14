# MealPilot – kostenloser Offline-Prototyp

Ein lokal nutzbares Rezeptbuch mit responsiver Oberfläche, Tagesplan und grober Nährwertberechnung. Keine Abos, externe Bibliotheken, Accounts oder kostenpflichtigen APIs erforderlich.

## Start

`index.html` in einem Browser öffnen. Alternativ für zuverlässiges Verhalten und Installation als Web-App lokal einen Webserver starten:

```bash
cd mealpilot
python3 -m http.server 8000
```

Dann `http://localhost:8000` öffnen. Für den Zugriff vom Smartphone aus ist eine statische HTTPS-Veröffentlichung, beispielsweise über GitHub Pages, erforderlich.

## Bereits nutzbar

- Eigene Rezepte inklusive Zutaten, Grammangaben, Zubereitung und Rezept-Quelllink speichern.
- Text aus TikTok-Beschreibungen in eine **editierbare** Rezeptkarte übernehmen; einfache Mengen und bekannte Zutaten werden erkannt.
- Pro-Portion-Nährwerte kalkulieren (kleine lokale Lebensmittelliste, **Schätzwerte**).
- Tagesplan mit wählbarem Datum und 2.000-kcal-Ziel.
- JSON-Export und -Import, einschließlich gespeicherter Tagespläne.
- Speicherung im lokalen Browser ohne Registrierung.

## Grenzen

- Der TikTok-Link wird derzeit nur als Quelle gespeichert. Es gibt **keinen vollautomatischen Video-, Audio- oder Bildimport**. Dazu ist ein zusätzlicher und technisch/rechtlich abgesicherter Importdienst notwendig.
- Fehlende Lebensmittel werden **nicht** mit erfundenen Nährwerten ergänzt, sondern als unvollständig markiert. Die angezeigte Kalorienzahl ist dann nur eine Untergrenze.
- Kein Mehrbenutzerbetrieb oder Synchronisierung zwischen Geräten. Dafür braucht es einen späteren Cloud-Dienst/Backend mit Authentifizierung und Nutzerrechten.
- Keine automatische Erstellung vollständiger Wochenpläne durch KI. Tagesplanung erfolgt manuell.
- PWA-Installation benötigt **HTTPS-Hosting** (oder `localhost`). Ein Service Worker und App-Icons sind bereits enthalten. Bei reiner Dateiöffnung kann die App nicht als echte PWA installiert werden.
- Die Werte sind nicht medizinisch geprüft; bei einzelnen Produkten die Packungsangaben nutzen.

## Nächste Schritte

1. Einfache Web-App öffentlich auf einem kostenlosen statischen Hosting bereitstellen.
2. Datenbank und Anmeldung (Freigaben, gemeinsame Rezeptbücher, Synchronisierung).
3. Technisch zulässigen TikTok-Rezeptimport mit Transkription und Prüfschritt entwickeln.
4. Verlässliche Lebensmitteldatenbank und optionale KI-Rezeptoptimierung.
5. Automatischen 7-Tage-Ernährungsplan und aggregierte Einkaufsliste ergänzen.

## Online-Veröffentlichung

Statische Dateien aus diesem Ordner öffentlich bereitstellen (z. B. GitHub Pages oder Render Static Site). App-Daten werden ausschließlich im jeweiligen Browser gespeichert; die öffentliche Website bietet noch keine Nutzerkonten oder geräteübergreifende Synchronisierung. Keine Links zu persönlichen Daten veröffentlichen.
