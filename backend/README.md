# MealPilot – Importservice (Gemini)

## Funktionen

- Extrahiert Rezepte aus **öffentlich zugänglichen TikTok-Videos**.
- Fragt TikTok-oEmbed nach Metadaten und lädt – soweit zugänglich und zulässig – die Videodatei zur Gemini File API hoch.
- Gemini wertet Bilder, Audio, Beschreibung und eingeblendete Texte aus und liefert strukturierte Rezeptkarten.
- Fehlende Grammangaben werden als 0 gekennzeichnet und **nicht erraten**.
- Nicht öffentlich verfügbare, private und gesperrte Inhalte können nicht analysiert werden; dann Rückfall auf öffentlich verfügbare Beschreibung.

## Bereitstellung

`backend/Dockerfile` als Render-Webdienst, Docker-Laufzeit, Free-Plan.
`GEMINI_API_KEY` ausschließlich als geheime Environment Variable in Render setzen – **nicht im Repository, nicht im Browser**.
`GEMINI_MODEL`: optional `gemini-2.5-flash`. HTTP-Endpunkte:
`GET /api/health`, `POST /api/import`.

Rate limit 6 Analysen je IP/Stunde und ein gleichzeitiger Job. Eine öffentliche API kann trotzdem missbraucht werden; für produktiven Betrieb Authentifizierung und ein festes Google-AI-Nutzungslimit einsetzen. Der Video-Import verursacht je nach Gemini-Nutzung Kosten. Render-Free-Dienst schläft bei Inaktivität; erster Import kann warten. Temporäre Videodateien werden nach der Analyse gelöscht und die Gemini-Datei nach der Anfrage zur Löschung vorgemerkt.

## Datenschutz

Die Videos werden für die Auswertung an Gemini/Google gesendet. Bei aktiviertem kostenlosen API-Kontingent beachte die entsprechenden Nutzungs- und Datenverarbeitungsbedingungen. Keine privaten Videos verwenden.