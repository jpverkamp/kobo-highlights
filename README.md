# Kobo Highlights Server

**Note: This server has *NO AUTHENTICATION*. It is designed to run on your own lan or behind a reverse proxy that handles this for you. Expose this to the Internet at your own risk.**

## Run the server (local dev)

```bash
cd server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Run the server (Docker)

```bash
docker build -t kobo-highlights ./server
docker run --rm -p 8000:8000 -v kobo-highlights-data:/data kobo-highlights
```

The server database is stored in the Docker volume at `/data/server.db`.

## Install on the Kobo (client)

1. Copy the client folder to your Kobo:

```bash
cp -R client /mnt/onboard/.adds/notes
chmod +x /mnt/onboard/.adds/notes/send-db.sh
```

2. Edit the on-device settings at the top of `send-db.sh`:

```sh
SERVER_BASE="http://kobo-server.local:8000"
```

## NickelMenu entries (with failure reason)

Main menu:

```txt
menu_item :main :Sync Notes :nickel_wifi :autoconnect
  chain_success :cmd_spawn :quiet :/mnt/onboard/.adds/notes/send-db.sh
  chain_success :dbg_toast :Notes synced
  chain_success :cmd_output :/bin/cat /mnt/onboard/.adds/notes/last_status.txt
  chain_failure :cmd_output :/bin/cat /mnt/onboard/.adds/notes/last_error.txt
  chain_failure :dbg_toast :Notes sync failed
```

Reader (in-book) menu:

```txt
menu_item :reader :Sync Notes :nickel_wifi :autoconnect
  chain_success :cmd_spawn :quiet :/mnt/onboard/.adds/notes/send-db.sh
  chain_success :dbg_toast :Notes synced
  chain_success :cmd_output :/bin/cat /mnt/onboard/.adds/notes/last_status.txt
  chain_failure :cmd_output :/bin/cat /mnt/onboard/.adds/notes/last_error.txt
  chain_failure :dbg_toast :Notes sync failed
```

Notes:
- `last_status.txt` and `last_error.txt` are updated by the client script.
- If the server is unreachable or the upload fails, the error reason will be written to `last_error.txt`.
