# Mazda 6e für Home Assistant

Custom Integration, die die Daten eines **Mazda 6e** (und CX-6e) aus der Cloud der
offiziellen App **„MAZDA 6e & CX-6e“** abruft und in Home Assistant anzeigt.

> Inoffiziell, nicht von Mazda unterstützt. Die API ist nicht dokumentiert und kann
> sich jederzeit ändern.

## Hintergrund (Recherche)

Der Mazda 6e basiert auf einer Plattform von Changan (Deepal SL03). Die App spricht
deshalb **nicht** mit dem alten MyMazda-Backend (das von `pymazda` genutzt wurde),
sondern mit Changans „CMA“-Gateway:

| Region | Basis-URL |
| --- | --- |
| Europa | `https://cma-m.iov.changanauto.com.de/cma-app-gw` |
| Asien/Pazifik | `https://cma.iov.changanauto.sg/cma-app-gw` |

Ablauf wie in der App:

1. `cma-app-auth/api/login/email-pass-in/v2` – E-Mail und Passwort werden mit einem
   in der App eingebetteten RSA-Schlüssel verschlüsselt; zusätzlich wird ein
   eigener öffentlicher Schlüssel (`pubKey`) registriert.
2. Bei einem neuen Gerät (`emailVerify: true`) schickt Mazda einen Code per E-Mail
   (`send-email/device-login/send`), der mit `login-device/email-verify` bestätigt wird.
3. Fahrzeugliste: `cma-app-user/api/car/vehicles` (bzw. `vehicle/vehicles`)
4. Status: `cma-app-car-condition/api/vehicle/condition/v2`
5. Token-Erneuerung: `cma-app-auth/api/auth/refresh-token`

Die Protokolldetails stammen aus den bestehenden Community-Projekten
[fano0001/home-assistant-mazda-6e](https://github.com/fano0001/home-assistant-mazda-6e) (Apache-2.0)
und [Sunek0/ha-mazda-6e-cx6](https://github.com/Sunek0/ha-mazda-6e-cx6) (MIT).

## Was angezeigt wird

| Bereich | Entitäten |
| --- | --- |
| Akku | Akkustand (%), Reichweite, Ladelimit |
| Laden | Ladestrom (A), AC/DC-Ladestrom (standardmäßig deaktiviert), Ladestatus, Restladezeit, Ladekabel eingesteckt, Lädt |
| Offen/Zu | Verriegelung, Türen (gesamt + einzeln), Kofferraum, Motorhaube, Fenster (gesamt + einzeln) |
| Sonstiges | Kilometerstand, Innentemperatur, Klimaanlage an, Fahrzeugzustand (fährt/geparkt), Reifendruck, Standort, Online-Status, Zeitpunkt der letzten Meldung |

Hinweis zur Verriegelung: Die Entität hat die Geräteklasse *Schloss*, d. h. **„Ein“ =
entriegelt**, „Aus“ = verriegelt.

Aktuell ist die Integration **nur lesend**. Fernsteuerung (Verriegeln, Klima,
Ladelimit) ist technisch möglich (signierte Befehle + 6-stelliger PIN) und kann
später ergänzt werden – der dafür nötige Schlüssel wird beim Login bereits erzeugt
und gespeichert.

## Installation

### HACS (benutzerdefiniertes Repository)

1. HACS → Integrationen → ⋮ → *Benutzerdefinierte Repositories* →
   `https://github.com/ghostsailorgit/mazda6e_homeassistant`, Kategorie *Integration*.
2. „Mazda 6e“ installieren, Home Assistant neu starten.

### Manuell

Ordner `custom_components/mazda6e` nach `<config>/custom_components/` kopieren und
Home Assistant neu starten.

## Einrichtung

*Einstellungen → Geräte & Dienste → Integration hinzufügen → Mazda 6e*

1. E-Mail, Passwort und Region eingeben (dieselben Daten wie in der App).
2. Mazda schickt einen **Bestätigungscode per E-Mail** – diesen eingeben.

Das Passwort wird nicht gespeichert, nur die Tokens. Unter *Konfigurieren* lässt sich
das Abfrageintervall (Standard: 5 Minuten) einstellen.

> Ob eine zusätzliche Anmeldung die App auf dem Handy abmeldet, ist nicht
> abschließend geklärt. Falls ja, hilft ein eigenes Zweitkonto, dem das Fahrzeug in
> der App freigegeben wird.

## Account ohne Home Assistant testen

```bash
pip install aiohttp cryptography
python scripts/mazda6e_cli.py --email du@example.com --raw
```

Das Skript loggt sich ein, fragt ggf. nach dem E-Mail-Code und gibt den Fahrzeugstatus
aus – ideal, um zu prüfen, ob alle Werte passen.

## Fehlersuche

- Debug-Log aktivieren:
  ```yaml
  logger:
    logs:
      custom_components.mazda6e: debug
  ```
- *Diagnose herunterladen* auf der Integrationsseite liefert die Rohdaten des
  Fahrzeugs (VIN, Standort und Tokens werden geschwärzt). Damit lassen sich
  unbekannte Felder zuordnen.

## Entwicklung

```bash
# API-Client und Datenaufbereitung (ohne Home Assistant)
pip install aiohttp cryptography pytest
pytest tests --ignore tests/ha

# Home-Assistant-Teil (Config-Flow, Entitäten) – separate venv, Python 3.13
pip install -r requirements_test.txt
pytest tests/ha
```

Die beiden Suites laufen getrennt, weil das HA-Test-Plugin Netzwerk-Sockets global
sperrt, die der Fake-Server der API-Tests braucht.
