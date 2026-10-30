# VPN Benchmark

Selbst gehostetes Tool zum Vergleichen von WireGuard-VPN-Exits. Es misst insbesondere Verbindungen in europäische Peer-Länder, Latenz, Paketverlust, TCP-Verbindungsrate, Handshake, MTU sowie globalen und regionalen Down-/Upload. Eine Weboberfläche zeigt Live-Fortschritt, Heatmaps, Verlauf, Diagnose und CSV-Exporte.

**WICHTIG: Nicht ins öffentliche Internet stellen!** Die Anwendung steuert Netzwerk-Namespaces mit erweiterten Linux-Capabilities, verwaltet WireGuard-Konfigurationen mit privaten Schlüsseln und kann ressourcenintensive Tests auslösen. Sie ist für den eigenen, vertrauenswürdigen Rechner beziehungsweise ein abgeschottetes privates Netz gedacht. Auch eine aktivierte HTTP-Basic-Authentifizierung macht einen direkten Internetzugang nicht sicher.

## Voraussetzungen

- Linux-Host mit aktuellem Docker Engine und Docker Compose v2
- WireGuard-Unterstützung im Linux-Kernel, /dev/net/tun und Berechtigung für Netzwerk-/Mount-Namespaces
- Ausreichend verfügbare Bandbreite und Traffic-Volumen
- Eigene gültige WireGuard-Clientkonfigurationen mit der Dateiendung .conf

Die Docker-Compose-Datei funktioniert ohne Unraid, ohne feste Host-IP und ohne host-spezifische Pfade. Rootless Docker, Docker Desktop sowie stark eingeschränkte Containerlaufzeiten können wegen der benötigten Netzwerk-Capabilities ungeeignet sein.

## Installation mit Docker Compose

Repository klonen und in das Verzeichnis wechseln:

~~~bash
git clone https://github.com/mlo-Tek/vpn-benchmark.git
cd vpn-benchmark
mkdir -p configs data
cp .env.example .env
docker compose up -d
~~~

Das Repository ist derzeit privat. Zum Klonen und zum Bezug des privaten Container-Images werden entsprechende GitHub-Berechtigungen benötigt. Bei GHCR gegebenenfalls zuerst anmelden:

~~~bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u DEIN_GITHUB_NAME --password-stdin
~~~

Den Token dabei nicht in der Shell-History oder im Repository speichern. Für reine Image-Downloads sind passende read:packages-Berechtigungen nötig.

**Standardmäßig hört die Weboberfläche nur auf 127.0.0.1:8765 des Docker-Hosts.** Direkt auf dem Host öffnen: http://127.0.0.1:8765

Von einem anderen Rechner aus über einen SSH-Tunnel:

~~~bash
ssh -L 8765:127.0.0.1:8765 nutzer@docker-host
~~~

Dann im lokalen Browser http://127.0.0.1:8765 öffnen. Alternativ über ein bereits abgesichertes privates VPN zum Docker-Host verbinden und dort einen SSH-Tunnel nutzen.

## Konfiguration

Die Datei .env wird nicht ins Git-Repository aufgenommen. Die Beispiel-Datei .env.example enthält:

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| VPN_BENCHMARK_BIND | 127.0.0.1 | Adresse, an die Docker den Web-Port bindet |
| VPN_BENCHMARK_PORT | 8765 | Port auf dem Host |
| PUID / PGID | 1000 / 1000 | Eigentümer für angelegte Dateien |
| TZ | Europe/Berlin | Zeitzone |
| VPN_BENCHMARK_USER / VPN_BENCHMARK_PASSWORD | leer | Optionale HTTP-Basic-Anmeldung; beide gemeinsam setzen |
| GITHUB_TOKEN | leer | Optionaler Read-only-Token zum Update-Check des privaten Repos |

Für die normale Installation muss kein Port im Router weitergeleitet werden. **Nicht 0.0.0.0, einen öffentlichen Reverse Proxy, ein offenes Ingress oder ein Portforwarding verwenden**, um die Anwendung internetöffentlich bereitzustellen. Auch die optionale Basic-Authentifizierung ist ohne TLS nicht vor Abhören geschützt und schützt nicht gegen alle Angriffe. Wenn Zugriff aus einem abgeschotteten LAN tatsächlich nötig ist, nur mit geeigneter Firewall und starken Zugangsdaten arbeiten; der Loopback-Standard plus SSH-Tunnel ist vorzuziehen.

Konfigurationen können über die Weboberfläche hochgeladen oder lokal unter configs/ abgelegt werden. Die Dateien sind wegen enthaltenen privaten WireGuard-Schlüsseln vertraulich. Der Container benötigt für Upload und Löschen Schreibzugriff auf configs/; data/ enthält die SQLite-Ergebnisse und optionale Messziele.

## Testphasen und Aussagekraft

1. **Baseline:** Vergleichsmessung ohne VPN, auf dem Docker-Host.
2. **Peering:** Parallele isolierte WireGuard-Namespaces (standardmäßig acht); Handshake, Exit-Informationen, MTU, TCP-Verbindungszeiten und -Fehler zu je vier Zielen in NO, NL, DE, SE, CH, FI, DK und PL.
3. **Durchsatz:** Seriell für die Finalisten (oder alle Configs im vollständigen Modus); globaler HTTPS-Download und -Upload, optional regionale iPerf3-Messungen.

Die Verbindungstests simulieren einen TCP-Swarm gegen öffentliche Webhosts. **Sie messen keine echten BitTorrent-Peers, kein UDP/uTP, keine Tracker-Interaktion und keine eingehende Portweiterleitung.** Ein hohes Ergebnis garantiert daher weder gute Torrent-Schwarm-Performance noch Port-Forwarding-Unterstützung. Für Seeding muss die eingehende Erreichbarkeit separat mit einem Port-Forward-Test und einem realen, legalen Test-Torrent überprüft werden.

Der HTTPS-Upload zählt nur abgeschlossene Upload-Anfragen, die vom Testserver mit HTTP-2xx bestätigt wurden. Abgebrochene oder abgewiesene Anfragen werden nicht als erfolgreicher Datentransfer gezählt. Das ist bewusst konservativ; insbesondere bei kurzen Testdauern kann der Wert unter der tatsächlich genutzten Leitungskapazität liegen. Öffentliche iPerf3-Server können überlastet, ausgefallen oder auf einen Client begrenzt sein. Fehlschläge und fehlende Messwerte sind daher kein sicherer Beweis für einen schlechten VPN-Exit.

## Ranking

In den Einstellungen zwischen folgenden Profilen wählen:

- **Seeding (Standard):** Upload priorisiert; globaler Durchsatz gewichtet 75 % Upload und 25 % Download, regionale Uploadtests stärker als Downloadtests.
- **Ausgewogen:** Globaler Durchsatz gewichtet 30 % Upload und 70 % Download.

Länder-Gewichte lassen sich ebenfalls einstellen. Gespeicherte Rohmessungen werden bei geänderten Gewichten beziehungsweise Profilen neu bewertet.

Wichtig: **Nur die Top-N erhalten im Standardmodus Durchsatzmessungen.** Resultate mit voller Durchsatzabdeckung werden deshalb vor vorläufigen Peering-only-Resultaten geführt. Die beiden Scores sind **nicht direkt vergleichbar**, ebenso wenig Testläufe mit unterschiedlich umfangreichen oder gescheiterten regionalen Messungen. Für einen fairen Endvergleich den vollständigen Modus wählen und nach Möglichkeit mehrere Läufe durchführen.

## Eigene Messziele

- targets.yaml: Eine eigene Datei unter data/targets.yaml überschreibt die im Image gebündelte Liste der TCP-Testziele.
- regions.yaml: Eine eigene Datei unter data/regions.yaml überschreibt die iPerf3-Testserver.

Die Formate sind in app/targets.yaml und app/regions.yaml dokumentiert. Für Länder-RTT möglichst regional verortete Ziele ohne weltweit verteiltes CDN nutzen, sonst kann ein CDN-PoP im falschen Land gemessen werden.

## Updates und Wartung

~~~bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
~~~

Für einen privaten GitHub-Update-Check kann ein fein begrenzter GITHUB_TOKEN in der lokalen .env gesetzt werden. Docker zieht Images nicht selbständig bei jedem neuen Commit; die obigen Befehle sind für Updates auszuführen. Die GitHub-Action prüft Tests und veröffentlicht das Image nach Änderungen auf main.

Lokale Verzeichnisse configs/ und data/ vor Updates sichern. **Niemals** private WireGuard-Keys, .env, personenbezogene Daten oder Logdateien mit Zugangsdaten in Issues, Pull Requests oder KI-Chats kopieren.

## Entwicklung und Tests

~~~bash
python -m pip install -r requirements.txt pytest httpx
python -m pytest -q
~~~

Die Unit-Tests decken unter anderem Parsing, Upload-Validierung, Rankings, Verlauf und iPerf3-Datenverarbeitung ab. Sie ersetzen keinen echten Integrationstest mit funktionierenden WireGuard-Konfigurationen, einem Linux-Host und realen Netzwerkzielen.

## KI-/AI-Hinweis

Teile des Quellcodes und der Dokumentation wurden mithilfe generativer KI-Assistenten erstellt oder überarbeitet. **KI-generierter Code kann Fehler und Sicherheitslücken enthalten.** Die Ergebnisse sind kein unabhängiger Sicherheits-Audit; insbesondere Messlogik, Netzwerkisolation und Privilegien müssen vor einem produktiven Einsatz selbst geprüft werden. Benchmark-Ergebnisse dienen als Entscheidungshilfe und sind keine Garantie für Performance, Anonymität oder Sicherheit.

## Haftung und Betrieb

Benutzung auf eigenes Risiko. Nur VPN-Profile und Testziele verwenden, für die eine Berechtigung besteht. Öffentliche Server fair benutzen; wiederholte Durchsatztests erzeugen beträchtlichen Datenverkehr.
