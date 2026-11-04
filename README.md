# Jízdní řády ČR — MCP server

Read-only MCP server nad soubory `data/merged` z repozitáře
[tangero/jizdni-rady-czech-republic](https://github.com/tangero/jizdni-rady-czech-republic),
který je zde připojený jako git submodul `upstream/`. Komunikuje
přes standardní vstup/výstup, proto nemá ani nepotřebuje síťový port.

## Nástroje

- `dataset_summary` — velikost a rozsah připojeného datasetu.
- `search_stops` — vyhledání zastávky, včetně `stop_id` a souřadnic.
- `search_routes` — vyhledání linek podle čísla či názvu.
- `departures` — plánované odjezdy ze zastávky pro datum a časové okno.
- `direct_connections` — přímé spoje mezi dvěma názvy zastávek; umí zvolit
  dnešní datum i běžné české pádové tvary názvů.

`departures` respektuje běžný GTFS kalendář i výjimky (`calendar_dates.txt`).
Dotazy jdou do SQLite indexu (`gtfs.sqlite`, ~220 MB), který se z GTFS souborů
sestaví předem (`python -m jizdni_rady_mcp.index <gtfs-adresář> <výstup.sqlite>`,
asi minutu). Odjezdy i přímé spoje pak trvají desítky milisekund. Docker image
obsahuje jen tento index, GTFS soubory v něm nejsou.

## Získání zdrojů

```powershell
git clone --recursive https://github.com/WallyCZ/jizdni-rady-cz-mcp.git
# nebo v existujícím klonu:
git submodule update --init
```

Na nejnovější data z upstreamu se submodul posune příkazem
`git submodule update --remote upstream` (v GitHub Actions to dělá každé úterý
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
`GTFS_DB`) a znovu, když jsou data novější než index.
