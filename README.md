# PCMT Playercams

Self-hosted playercam and persistent name-override service for the PCMT Spectra frontend.

The upstream Spectra Server stays unchanged. The PCMT frontend continues to receive normal match telemetry from Spectra on port 5200 and separately subscribes to this service for playercam state and name overrides.

## What this MVP implements

- Producer opens `/`, enters the current Spectra group code, and creates/reopens a playercam session.
- One random player join token is generated for the session.
- VDO room IDs use `pcmtplayercams` followed immediately by 12 randomized digits by default.
- All players use the same join URL, enter their full `Riot Name#Tagline`, then choose:
  - **Share Video** -> VDO.Ninja camera flow (`webcam2`)
  - **Share Image / File** -> VDO.Ninja media-file flow (`fileshare`)
- Publisher IDs match the existing PCMT frontend convention:
  - `Player Name#NA1` -> `Player_Name_H_NA1`
- The Spectra group code can be rebound without changing the VDO room or player join token.
- Already-connected PCMT frontends are pinned to the durable playercam session, so a group-code rebind does not detach their PCMT tools state.
- Registered Riot IDs are reported as `enabledPlayers` for compatibility.
- The producer can enable/disable displaying playercams without destroying the session; the room ID remains available so the frontend can keep feeds preloaded.
- Persistent name overrides are stored in `config/name-overrides.json` and keyed case-insensitively by full Riot ID.
- Saving an existing Riot ID updates/replaces its current override rather than creating a duplicate.
- The PCMT frontend reports the live Spectra roster back to this service, allowing overrides to be edited directly beside current-match players.
- Name-override edits are pushed immediately to all connected PCMT frontends.

## Run locally on Windows

```powershell
cd E:\Dev\PCMT-Playercams
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:CONFIG_DIR = "$PWD\config"
$env:DATA_DIR = "$PWD\data"
$env:PORT = "5400"
python -m uvicorn app.main:application --host 0.0.0.0 --port 5400 --reload
```

Open `http://localhost:5400`.

## Docker

```bash
docker compose up -d --build
```

The service listens on port `5400` by default.

## Configuration

`config/config.json`:

```json
{
  "publicBaseUrl": "http://localhost:5400",
  "sessionLifetimeHours": 48,
  "groupAliasGraceMinutes": 30,
  "roomPrefix": "pcmtplayercams",
  "roomRandomDigits": 12,
  "producerAccessKey": ""
}
```

`publicBaseUrl` defaults to `http://localhost:5400`. Like PCMT Mapban, change it directly in `config/config.json` when deploying behind a public hostname; for example, set it to the HTTPS URL used by players. Set `producerAccessKey` to a long random value if the producer page will be publicly reachable. The player-facing link has its own random join token and never exposes the Spectra group code as authentication.

If using Nginx Proxy Manager, proxy the public HTTPS hostname to this service on port `5400` and enable WebSocket support for Socket.IO.

### VDO room-name note

The default room ID is fully alphanumeric: `pcmtplayercams` followed immediately by 12 randomized digits (for example `pcmtplayercams482193750264`). The prefix and random-digit count remain configurable. Keep the generated room under 49 characters.

VDO.Ninja's `fileshare` mode is documented for video/audio media files. The PCMT UI intentionally keeps the requested **Share Image / File** wording, but static-image behavior should be live-tested in the target browser/VDO.Ninja version.

## Persistent overrides

The config file is intentionally human-readable:

```json
{
  "overrides": {
    "player name#na1": {
      "riotId": "Player Name#NA1",
      "displayName": "PlayerName"
    }
  }
}
```

The lowercase key is only the normalized lookup key. The original Riot ID casing is retained for the producer UI.

## Frontend protocol

The PCMT frontend connects by Socket.IO and emits:

```js
frontend_logon { groupCode }
```

The service replies and pushes updates as:

```json
{
  "groupCode": "ABC123",
  "sessionId": "PC-XXXXXXXXXX",
  "playercamsInfo": {
    "enable": true,
    "identifier": "pcmtplayercams482193750264",
    "enabledPlayers": ["Player Name#NA1"]
  },
  "nameOverrides": [["Player Name#NA1", "PlayerName"]]
}
```

If there is no PCMT playercam session for the group code, `playercamsInfo` is `null`. The frontend then falls back to the stock Spectra `tools.playercamsInfo`, while PCMT persistent name overrides can still apply.

The frontend also emits `frontend_roster` with the current teams/player Riot IDs. This data is transient and is not used as a player identity database.

## Tests

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

## GitHub Actions / release images

This repository includes the same release pattern used by PCMT Mapban:

- `.github/workflows/test-and-build.yml` runs on pushes and pull requests to `main`, installs Python 3.12 dependencies, compiles the source, runs the unit tests, validates config loading, and builds the Docker image.
- `.github/workflows/build-and-publish.yml` runs whenever a GitHub Release is published. It builds a multi-architecture image for `linux/amd64` and `linux/arm64` and publishes both `:latest` and the GitHub release tag to GitHub Container Registry (GHCR).

If this repository is created as `ianmdesign/PCMT-Playercams`, publishing release `v0.1.0` produces:

```text
ghcr.io/ianmdesign/pcmt-playercams:latest
ghcr.io/ianmdesign/pcmt-playercams:v0.1.0
```

The workflow uses the repository name dynamically, so no workflow change is required if the GitHub repository has a different name.

### Run a published release

There is a single `docker-compose.yml`. By default it names the published GHCR image as `ghcr.io/ianmdesign/pcmt-playercams:latest`, while retaining the local build context for development.

To pull and run the latest published release:

```bash
docker compose pull
docker compose up -d --no-build
```

To build and run directly from the checked-out source instead:

```bash
docker compose up -d --build
```

You can override the published image with the `PCMT_PLAYERCAMS_IMAGE` environment variable.

The `config` volume intentionally remains writable because `config/name-overrides.json` is part of the persistent service state. The SQLite session database is stored under `data/`.
