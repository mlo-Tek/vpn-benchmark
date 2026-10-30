# VPN Benchmark

VPN Benchmark ist eine selbst gehostete Webanwendung zum Vergleichen von **WireGuard-VPN-Exit-Servern**. Sie misst unter anderem Latenz, Paketverlust, TCP-Verbindungsaufbau, WireGuard-Handshake, MTU sowie Down- und Upload-Durchsatz. Die Ergebnisse lassen sich nach Peer-Ländern gewichten und in einer Weboberfläche vergleichen, speichern und als CSV exportieren.

> [!CAUTION]
> **SICHERHEIT – NICHT ÖFFENTLICH INS INTERNET STELLEN.** Die Anwendung verarbeitet WireGuard-Konfigurationen mit **privaten Schlüsseln** und benötigt Linux-Capabilities wie `NET_ADMIN` und `SYS_ADMIN`. **Keine öffentlichen Portfreigaben, kein öffentlich erreichbarer Reverse Proxy und kein offenes Ingress.** Die Standardkonfiguration bindet den Web-Port ausschließlich an `127.0.0.1` auf dem Docker-Host. Ein optionales HTTP-Basic-Passwort ersetzt weder TLS noch Netzwerkisolation.

> [!IMPORTANT]
> **KI-/AI-OFFENLEGUNG:** Wesentliche Teile dieses Projekts wurden mit Unterstützung **generativer KI** entwickelt und überarbeitet – einschließlich Python-Code, Mess- und Rankinglogik, Benutzeroberfläche, Docker-/CI-Konfiguration sowie Dokumentation. **Die Anwendung ist nicht unabhängig sicherheitsauditiert.** Auch bestandene automatisierte Tests schließen Fehler, Sicherheitslücken und ungenaue Messwerte nicht aus. Siehe [KI-Einsatz und Grenzen](#ki-einsatz-und-grenzen).

## Funktionen

- Mehrere WireGuard-Konfigurationen vergleichen, einschließlich Ordnerstruktur nach VPN-Anbieter.
- Pro Konfiguration isolierte Linux-Netzwerk-Namespaces und parallele Peering-Tests.
- Ziele in **NO, NL, DE, SE, CH, FI, DK und PL**: TCP-Verbindungszeiten und Ausfallraten.
- 100 parallele TCP-Verbindungsversuche zu Webhosts als synthetischer **TCP-Swarm-Test**.
- HTTPS-Down-/Uploadtests und optionale regionale iPerf3-Messungen.
- Wahl zwischen **Seeding** (uploadorientiert) und **Ausgewogen** als Ranking-Profil.
- Optional echte, *ausgehende* BitTorrent-Protokoll-Handshakes mit selbst angegebenen Test-Peers.
- Live-Status, Verlaufsdaten, Ländergewichtung, Diagnosen und CSV-Export.

## Voraussetzungen

- **Linux** mit Docker Engine und **Docker Compose v2**.
- WireGuard-Unterstützung im Linux-Kernel und verfügbares `/dev/net/tun`.
- Containerberechtigungen für Netzwerk- und Mount-Namespaces (`NET_ADMIN`, `SYS_ADMIN`).
- Eine oder mehrere gültige **WireGuard-Clientkonfigurationen** (`.conf`).
- Ausreichend Bandbreite und verfügbares Datenvolumen für Durchsatztests.

Rootless Docker, Docker Desktop auf macOS/Windows sowie restriktive Containerumgebungen sind wegen der erforderlichen Linux-Netzwerkfunktionen nicht als unterstützte Standardumgebung vorgesehen.

## Installation mit Docker Compose

**Repository und Docker-Image sind öffentlich.** Für das Klonen, den Image-Download und den Update-Check ist **weder ein GitHub-Konto noch ein GitHub-Token erforderlich**.

```bash
git clone https://github.com/mlo-Tek/vpn-benchmark.git
cd vpn-benchmark
mkdir -p configs data
cp .env.example .env
docker compose up -d
```

Das öffentliche Docker-Image wird über `ghcr.io/mlo-tek/vpn-benchmark:latest` bezogen. Die Compose-Datei verwendet relative Verzeichnisse und benötigt keine speziellen Hostpfade.

Auf **dem Docker-Host** lautet die Webadresse:

**http://127.0.0.1:8765**

Wer Docker auf einem anderen Linux-Rechner betreibt, kann von seinem lokalen Rechner einen SSH-Tunnel öffnen:

```bash
ssh -L 8765:127.0.0.1:8765 benutzer@docker-host
```

Danach im **lokalen** Browser `http://127.0.0.1:8765` öffnen. Es ist **keine** Portfreigabe am Router nötig.

### Verzeichnisse

| Pfad im Projekt | Pfad im Container | Inhalt |
| --- | --- | --- |
| `./configs/` | `/configs` | WireGuard-Konfigurationen (`.conf`), schützenswerte private Schlüssel |
| `./data/` | `/data` | Ergebnisse/SQLite-Datenbank sowie optionale Ziel- und Peer-Listen |

Bei einem bestehenden Docker-Stack mit **absoluten Volume-Pfaden** diese Mounts beibehalten, damit vorhandene VPN-Konfigurationen und Messergebnisse erhalten bleiben.

### Konfiguration über `.env`

`.env.example` enthält die verfügbaren Einstellungen. `.env` ist gitignoriert und darf nicht veröffentlicht werden.

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `VPN_BENCHMARK_BIND` | `127.0.0.1` | Host-IP für die Web-Portbindung; den Loopback-Standard beibehalten |
| `VPN_BENCHMARK_PORT` | `8765` | Web-Port auf dem Docker-Host |
| `PUID` / `PGID` | `1000` / `1000` | Eigentümer von durch die Anwendung angelegten Dateien |
| `TZ` | `Europe/Berlin` | Zeitzone |
| `VPN_BENCHMARK_USER` / `VPN_BENCHMARK_PASSWORD` | leer | Optionale HTTP-Basic-Anmeldung; beide Variablen gemeinsam setzen |

**Kein `GITHUB_TOKEN`:** Der integrierte Update-Check nutzt die **öffentliche GitHub-API ohne Anmeldung**. Falls die GitHub-API nicht erreichbar ist oder ihr anonymes Rate-Limit greift, wird der Update-Status lediglich als unbekannt angezeigt. Die GitHub Actions verwenden beim **Veröffentlichen** des Images intern einen automatisch bereitgestellten CI-Token; den müssen Nutzer weder erstellen noch konfigurieren.

Für API-Aufrufe, die Daten ändern (`POST`, `PUT`, `PATCH`, `DELETE`), ist der Header `X-VPN-Benchmark-Request: 1` erforderlich. Die Weboberfläche setzt ihn selbst. Dieser Schutz gegen einfache Cross-Site-Anfragen ist **kein Ersatz** für eine private Anbindung. Ein Upload erlaubt maximal 100 Konfigurationsdateien pro Anfrage und maximal 64 KiB pro Datei.

### WireGuard-Konfigurationen hinzufügen

Gültige `.conf`-Dateien über die Weboberfläche hochladen oder unter `./configs/` ablegen, optional nach VPN-Anbieter gruppiert. **Die privaten WireGuard-Schlüssel bleiben geheim**; Konfigurationen niemals in Git, Issues oder Logs veröffentlichen. Die Anwendung benötigt Schreibzugriff auf das Verzeichnis, weil die Weboberfläche Upload und Löschen unterstützt.

## Messablauf

1. **Baseline:** Vergleichsmessung ohne WireGuard-Tunnel **aus dem Docker-Container-Netzwerk**; dies ist nicht zwangsläufig identisch mit einer Messung direkt auf dem Linux-Host.
2. **Peering:** Paralleler Aufbau isolierter WireGuard-Tunnel (standardmäßig bis zu acht gleichzeitig); Handshake, Exit-Informationen, MTU, TCP-Zielmessungen und synthetischer TCP-Swarm.
3. **Durchsatz:** HTTPS-Down-/Upload, optional iPerf3 zu ausgewählten Ländern. In der üblichen Top-N-Auswahl werden nur Finalisten vollständig durchgemessen; der vollständige Modus kann alle Konfigurationen berücksichtigen.

**Messgrenzen:** Beim TCP-Swarm handelt es sich um HTTPS/TCP-Verbindungsversuche zu Webhosts – **nicht um einen echten Torrent-Schwarm**. Es werden standardmäßig keine Tracker, BitTorrent-Peers oder eingehenden Ports geprüft. Server, CDN-Routing, parallele Nutzer, VPN-Provider und Zeitpunkte können die Resultate erheblich beeinflussen.

Der HTTPS-Upload berücksichtigt nur vollständig gesendete Anfragen, die mit einem erfolgreichen HTTP-Status (`2xx`) bestätigt wurden. Bei kurzen Tests kann dieser konservative Ansatz niedriger ausfallen als die reale Leitungskapazität. Regionale öffentliche iPerf3-Server sind möglicherweise ausgelastet oder zeitweise unerreichbar.

### Optional: BitTorrent-Handshake-Test

Wer eigene, zulässige Test-Peers eines **legalen Test-Torrents** hat, kann diese ausdrücklich konfigurieren:

```bash
cp examples/bittorrent.yaml.example data/bittorrent.yaml
```

Danach im Ziel `data/bittorrent.yaml` die Beispielangaben (`host`, `port`, 40-stelliger `infohash`) ersetzen. Maximal 25 Peers. Ohne diese Datei werden keine echten BitTorrent-Peers kontaktiert.

Dieser Zusatztest prüft **nur ausgehende BitTorrent-TCP-Handshakes**, keine Datentransfers, kein UDP/uTP, keine Tracker-Suche und **keine eingehende Portweiterleitung**. Seine Resultate fließen nicht in das reguläre Ranking ein, weil einzelne Peers offline sein können.

## Ranking und Ergebnisse

- **Seeding (Standard):** Upload hat Vorrang. Beim globalen Durchsatzscore werden **75 % Upload und 25 % Download** gewichtet.
- **Ausgewogen:** Beim globalen Durchsatzscore werden **30 % Upload und 70 % Download** gewichtet.

Die Bewertung berücksichtigt daneben Peering, simulierte TCP-Verbindungsrate und Zuverlässigkeit. Die Länder-Gewichte können in der Weboberfläche eingestellt werden; gespeicherte Rohdaten werden beim Ändern des Profils erneut bewertet.

**Wichtig:** Peering-only-Ergebnisse und vollständige Durchsatzmessungen haben unterschiedliche Messabdeckung und sind **nicht direkt vergleichbar**. Für faire Vergleiche mehrere vollständige Durchläufe mit gleichen Testeinstellungen durchführen. Der Score ist kein Nachweis für Anonymität, erfolgreiche Portweiterleitung oder die tatsächliche langfristige Seeding-Leistung.

## Eigene Testziele

Bei Bedarf folgende Dateien anlegen, um die im Container enthaltenen Standardziele zu überschreiben:

| Datei auf dem Host | Wirkung |
| --- | --- |
| `data/targets.yaml` | TCP-Testziele pro Land, Vorlage unter [app/targets.yaml](app/targets.yaml) |
| `data/regions.yaml` | Regionale iPerf3-Server, Vorlage unter [app/regions.yaml](app/regions.yaml) |
| `data/bittorrent.yaml` | Optionale echte BitTorrent-Peer-Handshakes, Beispiel unter [examples/bittorrent.yaml.example](examples/bittorrent.yaml.example) |

Bei Ländertests auf tatsächlich regional verortete Ziele achten. Weltweit verteilte CDNs können in einer anderen Region antworten und die Länderbewertung verfälschen.

## Updates und Wartung

Im Verzeichnis der Compose-Datei:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

Die Weboberfläche kann **ohne GitHub-Token** per öffentlicher GitHub-API prüfen, ob es einen neueren Commit auf `main` gibt. Der Vergleich prüft **Commits, nicht den tatsächlichen Veröffentlichungsstand des Container-Images**. Ein neuer Commit kann daher bereits angezeigt werden, während das neue Docker-Image noch gebaut wird.

Das Image wird nach erfolgreich durchlaufenen automatisierten Tests von GitHub Actions nach GHCR veröffentlicht. Docker Compose aktualisiert laufende Container **nicht automatisch**; dafür `docker compose pull` und `docker compose up -d` ausführen. Vor Updates `./configs/` und `./data/` sichern. Bei bestehenden Stacks eigene Volume-Zuordnungen und Ports beibehalten.

## Entwicklung und Tests

```bash
python -m pip install -r requirements.txt pytest httpx
python -m pytest -q
```

GitHub Actions führt Unit-Tests aus und baut danach das Container-Image. **Ein grüner CI-Lauf belegt lediglich, dass diese Tests bestanden wurden.** Er ist kein Nachweis für sichere Containerprivilegien, korrekt funktionierende WireGuard-Namespaces auf allen Hosts, vollständige Netzwerksicherheit oder valide Messungen unter realen VPN-Bedingungen.

## KI-Einsatz und Grenzen

> [!WARNING]
> **Dieses Projekt wurde in wesentlichen Teilen KI-gestützt entwickelt.** Generative KI wurde nicht nur für Texte eingesetzt, sondern auch bei **Programmierung, Messverfahren, Scoring, Weboberfläche, Docker-/CI-Konfiguration und Änderungen am bestehenden Code**. Deshalb kann es trotz funktionierender Features und bestandener Tests **unentdeckte Fehler, Sicherheitslücken, unzutreffende Annahmen oder irreführende Benchmark-Ergebnisse** geben.

Die bisherigen Prüfungen und automatisierten Tests sind **kein unabhängiger Security-Audit**, keine formelle Codeprüfung und kein vollständiger End-to-End-Test auf beliebigen Linux-Hosts. Die Anwendung sollte ausschließlich in einer **kontrollierten, nicht öffentlich erreichbaren Umgebung** und mit VPN-Konfigurationen eingesetzt werden, für die eine Berechtigung besteht. Ergebnisse selbst plausibilisieren und sicherheitsrelevante Änderungen fachlich prüfen.

**Besonders wichtig:** Auch eine erfolgreich aufgebaute WireGuard-Verbindung bedeutet nicht automatisch, dass kein Datenleck auftritt, eingehende Torrent-Verbindungen funktionieren oder die reale Seeding-Leistung dem Score entspricht. Das Tool ist eine experimentelle Vergleichshilfe und kein Sicherheitsprodukt.

## Lizenz

Dieses Projekt ist unter der [MIT-Lizenz](LICENSE) veröffentlicht. Copyright © 2026 mlo-Tek. Nutzung, Modifikation und Weitergabe – auch kommerziell – sind bei Beibehaltung des Lizenz- und Copyright-Hinweises erlaubt.

## Haftung und verantwortungsvolle Nutzung

Benutzung auf eigenes Risiko und ohne Gewähr. Ausschließlich eigene oder zur Nutzung freigegebene VPN-Konfigurationen und Testziele verwenden. Öffentliche Testserver fair behandeln; wiederholte Durchsatztests können erheblichen Traffic und Kosten verursachen. **Keine privaten Schlüssel, Zugangsdaten, `.env`-Dateien oder sensiblen Logs in GitHub-Issues, Pull Requests oder KI-Chats posten.**
