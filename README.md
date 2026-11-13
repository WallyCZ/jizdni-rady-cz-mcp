# Jízdní řády ČR — MCP server

Read-only MCP server nad soubory `data/merged` z repozitáře
[tangero/jizdni-rady-czech-republic](https://github.com/tangero/jizdni-rady-czech-republic),
který je zde připojený jako git submodul `upstream/`. Komunikuje
přes standardní vstup/výstup, proto nemá ani nepotřebuje síťový port.

## Nástroje

- `dataset_summary` — velikost a rozsah připojeného datasetu.
- `search_stops` — vyhledání zastávky, včetně `stop_id` a souřadnic.
- `search_routes` — vyhledání linek podle čísla či názvu.
- `departures` — odjezdová tabule zastávky (název nebo `stop_id`): nejbližší
  odjezdy se směrem (konečnou), bez zadání dnes od teď na 3 hodiny dopředu.
  U pražských zastávek s aktuálním zpožděním (viz níže).
- `connections` — spojení mezi dvěma názvy zastávek, přímá i s přestupy
  (nejvýš `max_changes`, výchozí 4), výchozí 3 nejbližší. Vynechá spojení,
  které je horší v odjezdu, příjezdu i počtu přestupů než jiné nalezené; každý
  úsek s linkou, druhem dopravy, časy a přestupy pěšky. Umí dnešní datum,
  aktuální čas i běžné české pádové tvary názvů („z Drinopolu na Radlickou“).
  Název obce bez zastávky („do Větřkovic“) znamená kteroukoli její zastávku.
  Bez data a času hledá od teď, a když dnes už nic nejede, zítra od 4:00.

Oba nástroje respektují GTFS kalendář i výjimky (`calendar_dates.txt`). Časy
po půlnoci jsou podle GTFS větší než 24:00 (`24:03:00` je 0:03 dalšího dne).

## Rychlost

Dotazy jdou do SQLite indexu (`gtfs.sqlite`, ~430 MB), který se z GTFS souborů
sestaví předem (`python -m jizdni_rady_mcp.index <gtfs-adresář> <výstup.sqlite>`,
asi 1–2 minuty). Docker image obsahuje jen tento index, GTFS soubory v něm
nejsou. Server po startu index předehřeje (zastávky, chodníky, dnešní spoje).
Každé volání nástroje (parametry, doba, případná chyba) se zapíše do logu
kontejneru.

Spojení hledá algoritmus Connection Scan nad úseky spojů seřazenými podle
odjezdu. Čte jen úseky spojů, které ten den jedou (filtruje je SQLite). U cesty
delší než 40 km čte mimo okolí startu a cíle (20 km) jen dálkové spoje (vlaky
a linky s dosahem přes 30 km); místní spoje u startu jen první hodinu a půl,
u cíle až od nejdřívějšího možného příjezdu. Další spojení hledá znovu
přes už načtené úseky. Hledání má časový rozpočet: první spojení do 7 s, další
jen do 1,5 s; co nestihne, vrátí s `"partial": true`.

`scripts/bench_connections.py` změří sadu dotazů (index z `GTFS_DB`):

```powershell
python scripts/bench_connections.py 2026-11-13 12:00
```

Na vývojovém PC (13. 11. 2026, 5:00 / 12:00 / 19:15), před → po úpravě:

| Dotaz | Před | Po |
|---|---|---|
| Drinopol → Můstek | 0,3 s | 0,24–0,36 s |
| Drinopol → Větřkovice | 2,0–2,7 s, nic nenašel | 1,7–1,9 s, 2–3 spojení (19:15: žádné do půlnoci) |
| Televizní věž → Praha hl. n. | 0,3 s | 0,2–0,4 s |
| Praha hl. n. → Brno hl. n. | 1,4–2,9 s | 0,5–0,7 s |

Přestup na stejné zastávce trvá nejméně 2 minuty, pěšky se přestupuje
mezi zastávkami do 400 m vzdušnou čarou (1 m/s, zaokrouhleno na minuty).
Upstream data mají dvě vady, se kterými index počítá:

- obce stejného jména sdílejí jednu zastávku (Říčany u Prahy i u Brna), takže
  spoj z ní zdánlivě přeskočí přes republiku; úseky rychlejší než 200 km/h
  na vzdálenost nad 2 km se proto do plánovače nedostanou,
- 384 zastávek má souřadnice 0,0; z nich ani na ně se pěšky nepřestupuje.

## Aktuální zpoždění (Praha)

S proměnnou `GOLEMIO_API_KEY` (klíč zdarma po registraci na
[api.golemio.cz](https://api.golemio.cz)) server pro dnešní dotazy čte
odjezdové tabule PID z Golemio API. K odjezdům z pražských zastávek
(`departures` a jízdy v `connections`, které vyjíždějí do 3 hodin) přidá
`delay_minutes`, `realtime_departure` a `canceled` a do `summary` třeba
„(zpoždění 3 min)“. Odjezd páruje podle názvu zastávky bez „Praha, “, linky,
času podle jízdního řádu a směru. Tabule se drží 30 s; když Golemio do 2 s
neodpoví nebo klíč chybí, odpověď je jen podle jízdního řádu. Spojení se
podle zpoždění nepřepočítávají.

V Compose klíč předá `.env` vedle `compose.yaml` nebo proměnné stacku
v Portaineru:

```text
GOLEMIO_API_KEY=<váš klíč>
```

## Získání zdrojů

```powershell
git clone --recursive https://github.com/WallyCZ/jizdni-rady-cz-mcp.git
# nebo v existujícím klonu:
git submodule update --init
```

Na nejnovější data z upstreamu se submodul posune příkazem
`git submodule update --remote upstream` (v GitHub Actions to dělá každý den
workflow `update-upstream.yml`).

## Hotový image (GitHub Packages)

Workflow `docker.yml` při každém pushi do `main` sestaví image pro
`linux/amd64` i `linux/arm64` a publikuje ho jako
`ghcr.io/wallycz/jizdni-rady-cz-mcp:latest` (a `sha-<commit>`; tag `v1.2.3` přidá `1.2.3`).

```powershell
docker run --rm -i ghcr.io/wallycz/jizdni-rady-cz-mcp:latest
```

## Docker

```powershell
docker build -t jizdni-rady-cz-mcp .
docker run --rm -i jizdni-rady-cz-mcp
```

`compose.yaml` používá hotový image z GHCR (konkrétní verzi zvolí proměnná
`IMAGE_TAG`, výchozí je `latest`). Balíček je veřejný, přihlášení do GHCR
není potřeba:

```powershell
docker compose pull
docker compose --profile stdio run --rm -i mcp
```

Novější image stáhne `docker compose pull`. Lokálně sestavený image použije
Compose, když ho označíte stejným jménem
(`docker build -t ghcr.io/wallycz/jizdni-rady-cz-mcp:latest .`).

## Home Assistant

Home Assistant neumí přímo spouštět MCP server přes stdio. Služba `hass-mcp`
proto používá `mcp-proxy`, který interně spustí server přes stdio a vystaví
SSE endpoint. Spusťte ji takto:

```powershell
docker compose up -d hass-mcp
```

V Home Assistant otevřete **Settings → Devices & services → Add integration →
Model Context Protocol** a jako **SSE Server URL** zadejte:

```text
http://<IP-nebo-DNS-docker-hostu>:8000/sse
```

Je-li Home Assistant v jiném Docker Compose projektu na stejném hostiteli,
připojte oba projekty do stejné externí Docker sítě a použijte adresu
`http://hass-mcp:8000/sse`; port pak nemusí být zveřejněný na hostiteli.

Endpoint nyní nemá vlastní autentizaci. Zpřístupněte ho jen ve své důvěryhodné
síti; volitelně změňte veřejný hostitelský port proměnnou `MCP_PORT`.

Pro multiarch publikaci:

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --tag registry.example/jizdni-rady-cz-mcp:latest --push .
```

## Konfigurace MCP klienta

Příklad konfigurace pro klienta podporujícího MCP přes stdio:

```json
{
  "mcpServers": {
    "jizdni-rady-cz": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-i", "mcp"]
    }
  }
}
```

Pro lokální spuštění bez Dockeru vyžaduje Python 3.12+ (dataset je v
`upstream/data/merged`):

```powershell
python -m venv .venv
.venv\Scripts\pip install .
.venv\Scripts\jizdni-rady-cz-mcp
```

s proměnnou prostředí `GTFS_DATASET=upstream/data/merged`. Index se sestaví při
prvním dotazu do `~/.cache/jizdni-rady-cz-mcp/gtfs.sqlite` (jinou cestu určí
`GTFS_DB`) a znovu, když jsou data novější než index. Docker image má index
v `/usr/local/share/jizdni-rady-cz-mcp/gtfs.sqlite`, kde ho server najde i bez
proměnných prostředí (`mcp-proxy` spouští server s prázdným prostředím).
