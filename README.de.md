🇬🇧 **English version:** [README.md](README.md)

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
| Laden | Ladestrom (Summe Phasen), AC-Ladestrom/Akkustrom (standardmäßig deaktiviert), Ladestatus, Restladezeit, Ladekabel eingesteckt, Lädt |
| Offen/Zu | Verriegelung, Türen (gesamt + einzeln), Kofferraum, Motorhaube, Fenster (gesamt + einzeln) |
| Sonstiges | Kilometerstand, Innentemperatur, Klimaanlage an, Fahrzeugzustand (fährt/geparkt), Reifendruck, Online-Status, Zeitpunkt der letzten Meldung |

> Standort wird nicht angezeigt: Das Auto meldet in der Cloud-API keine Koordinaten
> (auch die offizielle App zeigt keinen Standort an), daher gibt es keine
> Standort-Entität.

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

## Weitere Fernsteuerung

Alle Befehle werden wie beim Verriegeln signiert. Entitäten für Ausstattung, die das Auto
laut seiner Funktionsliste (`function-config`) nicht hat, werden nicht angelegt.

| Entität | Was sie tut | PIN |
| --- | --- | --- |
| Knopf **Status vom Auto anfordern** | weckt das Auto, damit es sofort frische Daten meldet (nach ~30 s wird neu abgefragt) | – |
| Knopf **Lichthupe** / **Hupen und Blinken** | Auto auf dem Parkplatz finden | – |
| Regler **Ladelimit einstellen** | 60–100 % | – |
| Auswahl **Sitzheizung / Sitzlüftung Fahrer & Beifahrer** | Aus, Stufe 1–3 | – |
| Schalter **Lenkradheizung**, **Frontscheibe enteisen** | an/aus | – |
| Abdeckung **Fenster**, **Heckklappe** | öffnen/schließen | gespeicherte PIN nötig |
| Schalter + Uhrzeit **Akku vorheizen (Plan im Auto)** | der Vorheizplan, den auch die App zeigt | – |
| Schalter + Start/Ende **Ladezeitplan (Plan im Auto)** | der Ladeplan aus der App (nur wenn einer existiert) | – |

Fenster und Heckklappe fragen in Home Assistant keine PIN ab (Abdeckungen können das
nicht) – sie funktionieren nur mit gespeicherter Steuer-PIN, sonst kommt eine
Fehlermeldung.

Zusätzliche Sensoren: Luftfeuchte und Feinstaub im Innenraum, Lichter
(Abblend-/Fernlicht, Standlicht, Blinker – standardmäßig deaktiviert).

## Vorklimatisierung mit Wochenplan

Die Integration kann das Auto vor der Abfahrt vorheizen oder vorkühlen – nach einem
**Wochenplan mit eigener Abfahrtszeit pro Tag** und/oder ausgelöst durch **beliebige
Home-Assistant-Trigger**. Der Plan liegt in Home Assistant (nicht im Auto) und
funktioniert auch, wenn die Cloud kurz nicht erreichbar ist.

**Profil** (was beim Vorklimatisieren passiert, alles unter *Konfiguration* am Gerät):

- *Vorklimatisierung Temperatur* (16–30 °C)
- *Vorklimatisierung Vorlaufzeit* (5–30 min) – so lange vor der Abfahrt wird gestartet,
  die Klimatisierung läuft genau so lange
- *Vorklimatisierung: Sitzheizung* (Fahrersitz, Aus/1–3)
- *Vorklimatisierung: Lenkradheizung*, *…: Enteisen*
- *Vorklimatisierung: Akku vorheizen* – setzt den Akku-Vorheizplan des Autos immer auf die
  nächste geplante Abfahrt (der Akku braucht mehr Vorlauf, das regelt das Auto selbst)
- *Vorklimatisierung: Wetterquelle* – wähle eine `weather.*`-Entität, um nur bei Bedarf
  vorzuklimatisieren; *Aus* (Standard) klimatisiert immer vor
- *Vorklimatisierung: Mindesttemperatur* – ist eine Wetterquelle gewählt, läuft der
  Wochenplan nur, wenn die stündliche Vorhersage für die Abfahrtszeit unter diesem Wert
  liegt. Schlägt die Wetterabfrage fehl (Integrationsaussetzer, keine Vorhersagedaten),
  wird trotzdem vorklimatisiert – ein Fehler blockiert nie das Vorheizen

**Wochenplan:** Schalter *Vorklimatisierung Wochenplan* (Hauptschalter), dazu pro
Wochentag ein Schalter *Vorklimatisierung Montag…Sonntag* und eine Uhrzeit
*Abfahrt Montag…Sonntag*. Standard: Mo–Fr 07:30, Wochenende aus.

**Anzeige:** *Nächste Abfahrt* und *Nächster Vorklimatisierungsstart* (Zeitstempel; die
Attribute zeigen außerdem den letzten Lauf und fehlgeschlagene Schritte).

**Knöpfe:** *Vorklimatisierung starten* / *stoppen* (sofort, mit dem Profil) und
*Nächste Abfahrt überspringen* (z. B. Feiertag, Homeoffice).

### Externe Trigger (Automationen)

| Service | Zweck |
| --- | --- |
| `mazda6e.start_preconditioning` | jetzt vorklimatisieren; optional `temperature`, `duration`, `seat_heat` (0–3), `steering_wheel`, `defrost`, `battery` – leere Felder nehmen das Profil |
| `mazda6e.stop_preconditioning` | Klima, Sitz-/Lenkradheizung und Enteisen aus |
| `mazda6e.skip_next_departure` | nächste Abfahrt des Wochenplans auslassen |

`device_id` ist nur bei mehreren Autos nötig. `start_preconditioning` liefert als Antwort
die fehlgeschlagenen Schritte (`failed`). Jeder Lauf löst das Event
`mazda6e_preconditioning` aus (`action`: `started`/`stopped`, `source`:
`schedule`/`service`/`button`, `departure`, `failed`).

Beispiel: nach dem Kalender vorheizen, nur wenn es kalt ist

```yaml
automation:
  - alias: Mazda vorheizen vor Terminen
    triggers:
      - trigger: calendar
        event: start
        entity_id: calendar.arbeit
        offset: "-0:20:00"
    conditions:
      - condition: numeric_state
        entity_id: sensor.aussentemperatur
        below: 5
    actions:
      - action: mazda6e.start_preconditioning
        data:
          temperature: 22
          duration: 20
          seat_heat: 2
          steering_wheel: true
          defrost: true
```

Beispiel: Wochenplan an Feiertagen und im Urlaub aussetzen

```yaml
automation:
  - alias: Mazda Abfahrt an Feiertagen überspringen
    triggers:
      - trigger: time
        at: "20:00:00"
    conditions:
      - condition: state
        entity_id: binary_sensor.feiertag_morgen
        state: "on"
    actions:
      - action: mazda6e.skip_next_departure
```

Beispiel: Benachrichtigung, wenn ein Schritt fehlschlägt

```yaml
automation:
  - alias: Mazda Vorklimatisierung fehlgeschlagen
    triggers:
      - trigger: event
        event_type: mazda6e_preconditioning
    conditions:
      - "{{ trigger.event.data.failed | length > 0 }}"
    actions:
      - action: notify.mobile_app_handy
        data:
          message: "Vorklimatisierung: fehlgeschlagen {{ trigger.event.data.failed | join(', ') }}"
```

### Protokoll-Unsicherheiten

Die beiden Referenzprojekte unterscheiden sich an einigen Stellen; umgesetzt ist jeweils:

- Hupen/Blinken: `type` 1 = nur Licht, 3 = Licht + Hupe (laut Sunek0; fano nutzt 1 für
  „Auto finden“) – daher zwei Knöpfe
- Fenster: mit `openType: 10` (Sunek0)
- Signatur: ohne leeres `rcToken` bei Befehlen ohne PIN

Falls etwas davon am echten Auto nicht klappt, bitte mit Debug-Log melden.

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

Das Passwort wird nicht gespeichert, nur die Tokens und der Steuerschlüssel. Unter *Konfigurieren* lässt sich
das Abfrageintervall (Standard: 5 Minuten) einstellen.

> Mazda erlaubt pro Konto nur eine aktive Anmeldung: Meldet sich Home Assistant an,
> wird die App auf dem Handy abgemeldet (und umgekehrt). Abhilfe schafft ein
> **Zweitkonto nur für Home Assistant**:
>
> 1. In der App ein neues Konto mit einer anderen E-Mail-Adresse anlegen.
> 2. Mit dem Hauptkonto das Fahrzeug öffnen, auf *Teilen* tippen und das neue
>    Konto einladen.
> 3. Mit dem neuen Konto in der App anmelden und die Fahrzeugfreigabe annehmen.
> 4. Im neuen Konto eine eigene Steuer-PIN anlegen (z. B. beim Versuch, ein
>    Fenster zu öffnen – die App fragt dann danach). Die PIN gehört zum Konto,
>    das Hauptkonto-PIN gilt nicht für das Zweitkonto.
> 5. In der App wieder zum Hauptkonto wechseln. Ab jetzt nutzt nur noch Home
>    Assistant das Zweitkonto.

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
  Fahrzeugs (VIN und Tokens werden geschwärzt). Damit lassen sich unbekannte
  Felder zuordnen.

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
