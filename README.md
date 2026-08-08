# Batteribanken

Ett lokalt webgränssnitt för att registrera batterier, laddningar och verklig
kapacitet. Systemet använder SQLite och kräver ingen extern databas.

## Funktioner

- Batterityper lagras i databasen (exempelvis AA, AAA och 18650).
- Varje batterityp kan få egna extra fält.
- Batterier har unika ID:n, grunddata och status (`Aktiv`, `Väntande`, `Ej aktivt`).
- Sparade batterier kan redigeras, inklusive extra fält som läggs till på typen i efterhand.
- Laddningar sparar datum, kapacitet, läge, ström och valfri kommentar.
- CSV-data kan klistras in, mappas mot fält och förhandsgranskas före import.
- Hälsa beräknas från senaste uppmätta kapacitet / märkt kapacitet:
  grön över 97 %, gul över 80 % och röd därunder.

## Starta med Docker

```bash
cp .env.example .env
# Redigera SECRET_KEY i .env
docker compose up -d --build
```

Öppna sedan `http://servernamn:8000`. Databasen ligger i Docker-volymen
`battery-data`; ta backup med exempelvis `docker compose exec battery-tracker
cp /data/battery_tracker.db /data/backup.db`.

## Starta lokalt

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SECRET_KEY=valfri-hemlig-strang .venv/bin/python app.py
```

Öppna `http://localhost:8000`.

## Importera befintlig dokumentation

Välj `Importera` i huvudmenyn, välj importtyp och klistra in CSV-data. För batterier väljer
du först batterityp och mappar sedan rubrikerna mot dess fält. Importen validerar samtliga
rader före bekräftelse, så att inga data sparas om det finns fel. Semikolon rekommenderas
som avgränsare när värden använder svenska decimaler. Skriv `?` för en okänd valfri uppgift,
exempelvis introduktionsmånad; den importeras då som tom.

## Installera i Proxmox LXC

Kör ett kommando som `root` i Proxmox VE-hostens shell. Det skapar en oprivilegierad
Debian 12-LXC (1 CPU, 512 MiB RAM, 4 GiB disk, DHCP på `vmbr0`), installerar appen och
startar den som en systemd-tjänst:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/jockelandelius/Battery-tracker/main/deploy/proxmox-lxc.sh)"
```

För att välja container-ID, resurser, lagring eller brygga själv, hämta skriptet och
kör det med `--advanced`:

```bash
bash proxmox-lxc.sh --advanced
```

Skriptet kräver en amd64-baserad Proxmox VE-host med internetåtkomst. Appens data ligger
i containern under `/var/lib/battery-tracker`; säkerhetskopiera den filen regelbundet.
Sätt gärna en reverse proxy framför tjänsten för HTTPS. Det tidigare
`deploy/lxc-install.sh` finns kvar för manuell installation i en befintlig LXC.

## Uppdatera LXC-installationen

Öppna containerns terminal i Proxmox och kör som `root`:

```bash
update
```

Kommandot hämtar den senaste versionen från projektets `main`-gren på GitHub, uppdaterar
appfiler och beroenden och startar om tjänsten. Din databas i
`/var/lib/battery-tracker/battery_tracker.db` ändras inte.

För en container som skapades innan uppdateringskommandot fanns, kör en gång som `root`
inne i containern:

```bash
git clone --depth 1 https://github.com/jockelandelius/Battery-tracker.git /tmp/battery-tracker-source
cd /tmp/battery-tracker-source && bash deploy/lxc-install.sh
rm -rf /tmp/battery-tracker-source
```

Därefter finns kommandot `update` tillgängligt.
