# Stalker IPTV für Home Assistant

Integration für ein **eigenes, bezahltes** Stalker-/Ministra-Portal: Live-TV mit Programmführer (EPG), Filme und Serien,
mit Merkliste je Benutzer (Fortsetzen, gesehen).
Gespielt wird in der [Stalker Card](https://github.com/hypersonic30/ha-stalker-card), auch in der Companion App.

## Wie es funktioniert
- **Live-TV** kommt über die Stalker-API (Handshake mit MAC, Kanalliste, EPG, `create_link` erst beim Abspielen).
- **Filme und Serien** kommen aus M3U-Listen, die du angibst (optional). Serien werden über `serie-title`, `tvg-season`, `tvg-episode` gruppiert.
- Die Streams laufen **durch Home Assistant**: ffmpeg packt sie zu HLS um. **Video wird kopiert**, nur Audio wird zu AAC (Browser und Apps spielen kein DTS/AC3).
  Nur wenn ein Film einen Codec hat, den Browser nicht abspielen (z. B. HEVC), kodiert ffmpeg das Video nach H.264 um (höchstens 720p, `veryfast`).
  Das kostet deutlich mehr CPU (grob 1–2 Kerne); die Karte zeigt „umkodiert“ an. Live-TV wird nie umkodiert.
  So geht es auch unterwegs und ohne Mixed-Content-Probleme.
- Die Card sieht nur IDs. MAC-Adresse und Portal-Links bleiben in Home Assistant.

## Programmführer und Merkliste
- Der **Programmführer** kommt mit **einem** Portal-Aufruf (`get_epg_info`, ca. 5 MB für alle Kanäle) und wird 30 min zwischengespeichert.
  Daraus kommen „läuft gerade“ in der Senderliste und das Zeitraster der Karte (die nächsten ~30 Stunden).
- **Merkliste je Benutzer**: Position und „gesehen“ (ab 92 %) für Filme und Folgen, gespeichert in `.storage/stalker_progress`.
  Jeder Home-Assistant-Benutzer hat seine eigene; Titel und Staffel/Folge ermittelt der Server, die Karte sendet nur IDs und Zeit.
  Unter „Weiterschauen“ stehen angefangene Filme und je Serie die angefangene oder die nächste Folge.

## Schutz vor Überlast
- Standard: **1 Stream gleichzeitig**, höchstens 3. Ein neuer Stream beendet den alten, **bevor** das Portal einen neuen Link bekommt.
- Filme: ffmpeg wird angehalten, wenn er mehr als 80 s vor dem Player ist (CPU und RAM bleiben klein), HLS-Fenster 4 Minuten.
- Leerlauf: Live stoppt nach 30 s ohne Abruf, Filme nach 3 min. Die Card startet bei Bedarf an der letzten Position neu.
- ffmpeg läuft mit `nice -n 10`, die Abschnitte liegen in `/dev/shm`, Reste werden beim Start gelöscht.
- Portale, die ihre MACs rotieren („No MAC currently eligible, Retry in 8s“): Wiederholung nach 8 s, nur eine Verbindung je Stream.
- Streams dürfen nur von Servern kommen, die du selbst eingetragen hast (Portal und die beiden M3U-Adressen).

## Einrichtung
HACS → Benutzerdefiniertes Repository → Integration, dann *Einstellungen → Geräte & Dienste → Stalker IPTV*:
Portal-Adresse, MAC, optional die M3U-Adressen für Filme und Serien.

## Bekannte Grenzen
- Untertitel (PGS) und Tonspur-Wahl fehlen noch; bei Mehrfach-Ton wird Deutsch bevorzugt.
- Umkodierte Filme (HEVC) brauchen auf schwacher Hardware evtl. zu viel CPU und ruckeln; Spulen im Film startet den Stream neu (je nach Portal 5–20 s).
- Logos und Poster lädt die Card direkt vom Portal (im LAN ok).

Rechtlicher Hinweis: nur mit eigenem Abo verwenden.
