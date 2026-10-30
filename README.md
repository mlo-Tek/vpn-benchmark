# VPN Benchmark

Testet und vergleicht viele WireGuard-Configs auf Torrent-Tauglichkeit: Latenz und Loss zu den Ländern, in denen deine Peers sitzen (NO, NL, DE, SE, CH, FI, DK, PL), Peer-Swarm-Connect-Rate, Handshake, MTU, IPv6 und Durchsatz. Web-UI, läuft als Docker-Container auf Unraid.

## Ablauf eines Tests
1. **Baseline** ohne VPN (deine Leitung und dein ISP).
2. **Peering-Phase**, parallel (Standard 8 Tunnel gleichzeitig, jeder in eigenem Network-Namespace): Handshake, Exit-IP/Land, MTU, RTT/Loss zu je 4 Zielen pro Peer-Land, 100 parallele TCP-Verbindungen.
3. **Durchsatz-Phase**, seriell (die Leitung ist geteilt): Multi-Stream-Down/Upload nur für die Top-N (oder für alle im Modus "Alles voll").

Das Ranking gilt für *deinen* Anschluss: Deine Leitung begrenzt den Durchsatz, und das Peering deines ISP zum VPN-Anbieter fließt in jede Messung ein. Die Länder-Matrix (Exit → Peer-Land) ist davon weitgehend unabhängig.

## Unraid-Installation (privates Repo)
1. **GitHub-Token** erstellen: GitHub → Settings → Developer settings → Personal access tokens (classic) → Scope `read:packages`.
2. Auf dem Unraid-Terminal einloggen:
   ```bash
   docker login ghcr.io -u <github-user>
   ```
   (Token als Passwort.)
3. Template ablegen:
   ```bash
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   curl -H "Authorization: token <TOKEN>" -L \
     https://raw.githubusercontent.com/mlo-Tek/vpn-benchmark/main/unraid/vpn-benchmark.xml \
     -o /boot/config/plugins/dockerMan/templates-user/my-vpn-benchmark.xml
   ```
4. Unraid → Docker → *Add Container* → Template `vpn-benchmark` wählen → Apply.
5. Deine `.conf`-Dateien nach `/mnt/user/appdata/vpn-benchmark/configs` kopieren, UI auf `http://<unraid-ip>:8080` öffnen.

**Updates:** Jeder Push auf `main` baut per GitHub Actions ein neues Image. In Unraid → Docker → *Check for Updates* erscheint dann "update ready", dort "apply update".

Der Container braucht `--cap-add=NET_ADMIN --cap-add=SYS_ADMIN --device=/dev/net/tun` (steht im Template), kein `--privileged`. Das Unraid-Host-Kernel muss WireGuard unterstützen (ab Unraid 6.10 der Fall).

## Anpassen
- **Ziele pro Land:** `app/targets.yaml` kopieren nach `/data/targets.yaml` und editieren. Nimm Hosts, die nicht hinter einem globalen CDN liegen, sonst misst du den nächsten CDN-Edge statt des Landes.
- **Länder-Gewichte:** in der UI unter "Länder-Gewichte". Die Rangliste wird sofort neu berechnet, auch für alte Läufe.

## Sicherheit
`*.conf` steht in `.gitignore`, damit keine Private Keys im Repo landen. Die Configs werden nur lesend gemountet.

## Entwicklung
```bash
pip install -r requirements.txt pytest httpx
pytest
```
