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

## Fernverriegelung

Die Entität **`lock.<auto>_turen`** („Türen“) verriegelt und entriegelt das Auto aus der
Ferne – wie der Schloss-Knopf in der App. Dafür wird die **6-stellige Steuer-PIN** aus
der Mazda-App benötigt. Zwei Varianten:

- **PIN nicht speichern (Standard):** Home Assistant fragt bei jedem Ver-/Entriegeln
  nach der PIN.
- **PIN speichern:** *Integration → Konfigurieren → „Steuer-PIN speichern“* und PIN
  eingeben. Sie wird beim Speichern direkt bei Mazda geprüft. Danach funktionieren
  auch Automationen (z. B. „Um 22 Uhr verriegeln, wenn offen“). Achtung: Wer
  Zugriff auf Home Assistant hat, kann das Auto dann entriegeln.

Ablauf im Hintergrund (wie die App): Anzahl verbleibender PIN-Versuche abfragen →
PIN gegen ein `rcToken` tauschen → verschlüsselte Einmal-Seriennummer holen und mit
dem beim Login registrierten Schlüssel entschlüsseln → Befehl mit RSA-SHA256 signieren
und an `control/doors` senden → `control/control-result` abfragen, bis das Auto
bestätigt (max. 90 s).

Sicherheitsnetz: Sind keine PIN-Versuche mehr übrig, wird die PIN gar nicht erst
gesendet. Bei falscher PIN zeigt die Fehlermeldung die verbleibenden Versuche.

Nach einem bestätigten Befehl zeigt die Entität den neuen Zustand sofort an, bis das
Auto selbst einen neueren Status meldet (spätestens nach 10 Minuten).

> Das Auto braucht Mobilfunkempfang. In Tiefgaragen kann ein Befehl mit
> „nicht rechtzeitig bestätigt“ fehlschlagen.

## Klimasteuerung

Die Entität **„Klimatisierung“** (`climate.<auto>_klimatisierung`) startet und stoppt die
Standklimatisierung wie der Lüfter-Knopf in der App – z. B. um das Auto im Winter
vorzuheizen oder im Sommer vorzukühlen.

- Modi: *Aus* und *Heizen/Kühlen*, Zieltemperatur 16–30 °C in 0,5er-Schritten
- Die Klimatisierung läuft jeweils **15 Minuten** (wie der Standard in der App)
- Keine PIN nötig (die App fragt dafür auch keine ab)
- Temperatur ändern, während die Klimatisierung aus ist, merkt sich nur den Wert und
  startet nichts – erst „Einschalten“ startet mit dieser Temperatur
- Aktuelle Temperatur = Innenraumtemperatur, die das Auto meldet

Beispiel-Automation: werktags um 7:15 Uhr auf 21 °C vorheizen

```yaml
automation:
  - alias: Mazda vorheizen
    triggers:
      - trigger: time
        at: "07:15:00"
    conditions:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.mazda_6e_klimatisierung
        data:
          temperature: 21
          hvac_mode: heat_cool
```

## Installation

### HACS (benutzerdefiniertes Repository)

> HACS kann nur öffentliche GitHub-Repositories installieren. Solange dieses
> Repository privat ist, bitte die manuelle Installation verwenden.

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

Das Passwort wird nicht gespeichert, nur die Tokens und der Steuerschlüssel. Unter *Konfigurieren* lässt sich
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
aus – ideal, um zu prüfen, ob alle Werte passen. Mit `--lock` bzw. `--unlock` lässt sich
die Fernverriegelung testen (fragt nach der Steuer-PIN), mit `--climate-on 21` bzw.
`--climate-off` die Klimasteuerung.

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
