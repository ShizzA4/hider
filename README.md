# Ghostbot

Discord slash-command bot that repackages a Fabric mod jar under another mod's identity.

It copies the cover's name, version, description, authors, contact, license, icon, and language files into the source jar while preserving the source's entrypoints, mixins, access widener, dependencies, nested jars, and `custom` metadata.

## What it does

- Reads `fabric.mod.json` from both jars.
- Rebuilds the source metadata with the cover's visible identity.
- Copies the cover icon to its declared path.
- Optionally overwrites the source's own icon with the cover icon, resized to the source icon's dimensions.
- Copies the cover's `assets/<namespace>/lang/*.json`.
- Keeps the source's functional metadata: `entrypoints`, `mixins`, `accessWidener`, `depends`, `recommends`, `suggests`, `breaks`, `conflicts`, `jars`, `languageAdapters`, `custom`.
- Replaces the manifest with its main section only, dropping stale per-entry digests.
- Strips `.SF`, `.RSA`, `.DSA`, `.EC`, and `SIG-*` signature entries.
- Repacks and verifies the output zip.

## Setup

1. Create an application at <https://discord.com/developers/applications>.
2. Add a bot, copy the token.
3. Invite it with the `bot` and `applications.commands` scopes. No privileged intents are needed.
4. Install Python 3.10+.
5. Install dependencies:

```bash
cd ghostbot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

6. Create `.env` and fill it in:

```powershell
Copy-Item .env.example .env
notepad .env
```

```dotenv
DISCORD_TOKEN=your-token
GUILD_ID=
ALLOWED_USER_IDS=
```

7. Start it:

```powershell
python bot.py
```

Or use `run.bat` on Windows. It creates `.env` from the example if the file is missing.

`GUILD_ID` is optional. Without it the command syncs globally and can take up to an hour to appear.

`ALLOWED_USER_IDS` is optional but recommended for a private bot. It is a comma-separated list of Discord user IDs. Leave it empty to let anyone in the server use `/ghost`. Only give access to people you trust with arbitrary jar processing.

## Command

```text
/ghost source:<jar> cover:<jar>
```

Options:

| Option | Default | Meaning |
|---|---|---|
| `source` | required | Jar to hide |
| `cover` | required | Jar to impersonate |
| `id_mode` | `keep` | `keep` = source ID, `cover` = cover ID, `custom` = `custom_id` |
| `custom_id` | none | Required when `id_mode` is `custom` |
| `version` | cover version | Output version string |
| `filename` | cover attachment's filename | Output filename |
| `replace_icon` | true | Overwrite the source's own icon too |
| `copy_lang` | true | Copy cover language files |

### ID modes

- `keep` — keeps the source mod ID. Safest for clients that call `FabricLoader.getModContainer("<source-id>")` to locate their own jar. Visible name, version, and icon still match the cover.
- `cover` — uses the cover mod ID. Best disguise, but do not install the real cover jar alongside it; Fabric rejects duplicate mod IDs.
- `custom` — uses `custom_id`. Useful when the cover ID would collide with an installed mod, e.g. `fastclient-addon`.

Examples:

```text
/ghost source:orchard-1.21.11.jar cover:dark-loading-screen-1.6.17.jar id_mode:keep
```

```text
/ghost source:anubis.jar cover:fastclient-addon-1.21.11.jar id_mode:custom custom_id:fastclient-addon
```

The bot reuses the cover attachment's filename by default, so the output keeps the cover-style name. The CLI defaults to `<cover-id>-<version>.jar` and accepts `--filename`.

## CLI

The same engine runs without Discord:

```bash
python ghoster.py source.jar cover.jar -o out
python ghoster.py source.jar cover.jar -o out --id-mode custom --custom-id fastclient-addon
python ghoster.py source.jar cover.jar -o out --id-mode cover --version 1.6.17
```

## Railway

Railway is the easiest always-on host for this bot. It detects the Dockerfile, runs the worker, and restarts it if the process crashes. `railway.json` pins the Docker builder and the restart policy.

1. Push `ghostbot` to a GitHub repo. `.gitignore` keeps `.env` and `.venv` out.
2. Railway -> New Project -> Deploy from GitHub repo -> select the repo.
3. Open Variables and add:

```dotenv
DISCORD_TOKEN=your-token
GUILD_ID=your-server-id
ALLOWED_USER_IDS=your-user-id
```

4. Deploy. The logs should show `logged in as ...` and the invite URL.
5. No public port is needed. This is a gateway worker, not a web app.

Cost: the Hobby plan is $5/month including $5 of usage; this bot usually fits inside that credit. Railway does not sleep Hobby services. Free web hosts that sleep will drop the Discord gateway connection.

CLI alternative:

```bash
npm i -g @railway/cli
railway login
cd ghostbot
railway init
railway up
railway variables --set DISCORD_TOKEN=your-token
```

Then add `GUILD_ID` and `ALLOWED_USER_IDS` the same way, or set them in the dashboard.

## Notes

- Output is unsigned. Remove any real signature checks before relying on a repacked jar.
- Only metadata, icon, and language files are changed. Classes, packages, mixin configs, and log strings stay as the source.
- Source `custom` metadata is preserved because clients often read it. This can retain source-specific keys inside `fabric.mod.json`.
- The bot responds ephemerally; the uploaded jar is visible only to the invoker.
- Temp files are deleted after each command.
- Discord's upload cap is read from the guild and capped at 25 MB.
- Never commit or share `DISCORD_TOKEN`.
