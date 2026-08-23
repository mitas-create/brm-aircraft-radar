# BRM Aero Aircraft Radar

Lokální webová appka pro sledování letadel — provozuje Petr Mitáš (BRM Aero, Kunovice).
Komunikace s uživatelem probíhá **česky**. Text uvnitř appky je **anglicky**.

## Spuštění

```
start-lokalne.bat        # jen appka (běžné použití)
start.bat                # appka + veřejný odkaz přes Cloudflare Tunnel (potřebuje tools/cloudflared.exe)
python fleet_poller.py   # jedno kolo kontroly flotily, bez serveru (používá GitHub Actions)
```

Appka běží na `http://127.0.0.1:8765/`. Vyžaduje jen Python 3, žádné balíčky —
schválně jen standardní knihovna.

## Struktura

Celá appka je **jeden soubor** `lkku_radar_server.py` — Python server, uvnitř kterého
je jako string vložená celá HTML stránka (`HTML_PAGE`). Je to záměr: uživatel má jeden
soubor, který spustí, nic se neinstaluje.

## Tři panely

1. **LKKU Radar** — letadla do 30 NM od Kunovic (49.0294 N, 17.4397 E)
2. **Bristell Radar** — všechny Bristelly ve vzduchu na světě (typy `BR23`, `NG5`, `BR8`, `B23E`)
3. **BRM Aero Fleet** — 7 sledovaných registrací + logbook letů

## Datové zdroje a jejich úskalí

| Zdroj | K čemu | Pozor na |
|---|---|---|
| `opendata.adsb.fi/api/v3/lat/../lon/../dist/..` | letadla u LKKU | **neposílá CORS** → nutný server-side proxy |
| `api.adsb.lol/v2/type/{TYP}` | Bristelly celosvětově | **neposílá CORS** |
| `api.adsbdb.com/v0/callsign/{CS}` | trasa letu + aerolinka | zná **jen linkové lety**, ne GA |
| `api.adsbdb.com/v0/aircraft/{REG}` | vlastník podle registrace | u malých letadel většinou „unknown" |
| `tgftp.nws.noaa.gov/.../LKKU.TXT` | METAR | LKKU hlásí zřídka → často „NOT AVAILABLE" |
| OurAirports `airports.csv` | nejbližší letiště (~80k) | 12 MB, cachuje se lokálně 30 dní |
| `api.bigdatacloud.net/.../reverse-geocode-client` | země podle pozice (vlaječky) | cachuje se na mřížce ~55 km |

**Nepoužívat `api.airplanes.live`** — od roku 2026 vrací 403 a vyžaduje registraci.
Proto se přešlo na adsb.fi / adsb.lol.

## Rozhodnutí, která je dobré znát

- **CORS je důvod, proč vůbec existuje ten Python server.** Ověřeno — adsb.fi ani
  adsb.lol neposílají `Access-Control-Allow-Origin`, takže statická stránka
  (GitHub Pages) živý radar zobrazit nedokáže. Šlo by to jen přes vlastní proxy.
- **„Operator" se bere z volacího znaku, ne z registrace.** Prefix callsignu
  (RYR → Ryanair) je mnohem spolehlivější. Lookup podle registrace je jen záloha pro GA.
- **Departure/Destination u Bristellů** se neodvozuje z trasy (ta pro GA neexistuje),
  ale z nejbližšího letiště k aktuální pozici. Když je letadlo nízko (< 3000 ft) a jasně
  stoupá/klesá, přiřadí se jako odlet/přílet; jinak se zobrazí **žlutě jako „(probable)"**.
- **Logbook se počítá v Pythonu, ne v prohlížeči.** Kdyby se logika psala ještě jednou
  v JS, začaly by se obě kopie rozcházet. Případná statická stránka má dostat hotová data.

## Sledování flotily

7 registrací v `BRM_FLEET_REGISTRATIONS`, z toho **4 jsou zalétávací značky**
(`OK-DUI90`, `OK-QUU06`, `OK-VAU99`, `D-MZYW`) používané při továrních zálety před
předáním zákazníkovi.

**Účel hlídání duplicit:** zákazník si občas zapomene přepsat transpondér a létá pak
po světě s tovární značkou. Když se jedna registrace objeví na dvou různých hex kódech,
řádek se červeně označí a událost se uloží do `duplicate_events`.

### Detekce letů (logbook)

Stavový automat `_track_flight()` sleduje přechod `on ground ↔ airborne`:
- vzlet i přistání zachyceno → normální řádek
- signál chycen jen zčásti → **žlutý řádek** + vysvětlující poznámka v tooltipu
- časy v místním čase daného místa se značkou `LT` (odhad podle země, jinak `UTC`)

## Nepřetržité logování přes GitHub Actions

Repo: <https://github.com/mitas-create/brm-aircraft-radar> (**veřejné**)

`.github/workflows/fleet-poll.yml` spouští `fleet_poller.py` každých 15 minut a
commituje `brm_fleet_log.json` zpátky. Běží i s vypnutým počítačem.

Appka si tenhle vzdálený log **stáhne a sloučí** s lokálním (`_load_merged_log()`),
takže logbook je kompletní bez ohledu na to, kde a kdy se appka pustí.
Sloučení nikdy nepřepisuje lokální soubor — je jen pro zobrazení.

Shoda letů se pozná podle registrace + vzletu do 25 minut (`SAME_FLIGHT_TOLERANCE_MIN`),
při shodě vyhrává úplnější záznam, ale ručně zapsaný pilot se zachová.

## Na co si dát pozor

- **`brm_pilots.json` nesmí do gitu.** Jsou to jména konkrétních lidí a repo je veřejné.
  Je v `.gitignore`. Důsledek: jména se nepřenášejí mezi počítači.
- **Commity používají noreply adresu** (`mitas-create@users.noreply.github.com`),
  aby se ve veřejné historii neobjevil osobní e-mail.
- **Cron u veřejného repa může být á 15 min** (minuty Actions jsou neomezené).
  U privátního by se to nevešlo do 2000 min/měsíc → tam by muselo být 30 min.
- GitHub cron **není přesný**, ve špičce nabíhá o 10–30 min později. Pro hlídání
  duplicit to nevadí, časy v logbooku jsou tím ale hrubší než při lokálním běhu (5 min).
- Když se v repu 60 dní nic neděje, GitHub naplánované workflow vypne.

## Co je rozdělané / nápady dál

- **Sdílená stránka (GitHub Pages)** — Fleet + Logbook by šly zobrazit staticky
  z předpočítaného JSON. Živý radar tam kvůli CORS nepůjde bez vlastní proxy.
- **Sdílení jmen pilotů mezi počítači** — dnes jsou jen lokální.
- Panel BRM Aero Fleet umí i dotaz do rejstříku ÚCL
  (`https://lr.caa.gov.cz/api/avreg/filtered?search=owner~^~BRM Aero`) — zatím se
  nepoužívá, ale API je veřejné a funkční, vrací i transpondérové adresy.
