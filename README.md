# 🩻 rp-connect

A self-contained Docker stack for submitting cases to [Radiopaedia](https://radiopaedia.org) with **Radiopaedia Connect**, using a local **Orthanc** PACS as the DICOM source.

```mermaid
flowchart LR
    you(["🧑‍⚕️ You<br/>(browser)"])
    pacs(["🏥 PACS / viewer"])

    subgraph stack ["docker compose"]
        caddy["🔒 Caddy<br/>HTTPS proxy"]
        rp["📤 radiopaedia-connect<br/>AE: RCONNECT_SCP"]
        orthanc["🗄️ Orthanc<br/>AE: ORTHANC"]
    end

    rp_cloud(["☁️ radiopaedia.org"])

    you -- "https://localhost:8443" --> caddy --> rp
    you -- "http://localhost:8042" --> orthanc
    pacs -- "C-STORE → :4242" --> orthanc
    rp -- "C-FIND / C-MOVE" --> orthanc
    orthanc -- "C-STORE → :4104" --> rp
    rp -- "upload" --> rp_cloud
```

---

## 🌐 Where to find everything

| What | Address | Notes |
|---|---|---|
| **Radiopaedia Connect** (web UI) | **<https://localhost:8443>** | Served by Caddy with its own local certificate. Accept the browser warning the first time. |
| **Orthanc Explorer 2** (web UI) | **<http://localhost:8042/ui/app/>** | Browse, upload and delete studies. No login. |
| Orthanc classic explorer | <http://localhost:8042/app/explorer.html> | The older UI. |
| Orthanc REST API | <http://localhost:8042/> | e.g. `/statistics`, `/modalities` |
| **Orthanc DICOM** | `localhost:4242`, AE **`ORTHANC`** | Send studies here from your PACS or viewer. |
| Radiopaedia Connect DICOM | `localhost:4104`, AE **`RCONNECT_SCP`** | Listener port set by `RCONNECT_SCP_PORT=4104`. |

> 💡 All three services use host networking. They reach each other through `127.0.0.1`, not Docker service names. There is no port mapping: Connect listens directly on `4104`, and Caddy proxies to `127.0.0.1:5000`.

Host networking works natively on Linux. On Docker Desktop 4.34 or later, enable **Settings → Resources → Network → Enable host networking** for Linux containers. Ensure ports `4104`, `4242`, `5000`, `8042`, `8080`, and `8443` are available.

> ⚠️ Host networking exposes listeners directly on the host. Orthanc has authentication disabled and remote access enabled. Restrict access to trusted machines with your host firewall, particularly ports `8042`, `5000`, `4104`, and `4242`.

---

## 🚀 Quick start

Create the admin password file described in **Browser-free setup with Docker** below **before starting the stack**. The configuration helper runs automatically with `docker compose up`.

```bash
# 1. Create the host folders and a fixed host ID for Orthanc (first time only)
mkdir -p data/radiopaedia-connect data/orthanc data/orthanc-export orthanc.d
[ -f data/orthanc-hostid ] || printf '%x\n' $(shuf -i 268435456-4294967295 -n 1) > data/orthanc-hostid

# 2. Start the stack
docker compose up -d

# 3. Check that everything is up
docker compose ps
```

> ⚠️ Create `data/orthanc-hostid` **before** the first `docker compose up`. If it's missing, Docker creates a *folder* with that name and Orthanc won't start.

Then either use the browser-free setup below or open **<https://localhost:8443>** and work through the web setup.

### Migrating an existing bridge-network deployment

1. In Connect's **Settings → Remote Nodes**, edit the existing Orthanc node: change **Host / IP** from `orthanc` to `127.0.0.1`, leaving port `4242` and the AE titles unchanged. This setting is saved in the database, so Compose cannot update it.
2. Update any machine-local modality pointing at an old Docker bridge gateway to the appropriate host address (`127.0.0.1` for a viewer on the same host).
3. Recreate the services:

   ```bash
   docker compose config --quiet
   docker compose up -d --force-recreate
   docker compose logs --tail=100 radiopaedia-connect
   ```

4. Confirm the Connect DICOM listener reports port `4104`. Test the Orthanc node's **DICOM C-ECHO (ping)** in Connect, then test the reverse direction:

   ```bash
   curl --fail-with-body -X POST http://localhost:8042/modalities/RCONNECT_SCP/echo -d '{}'
   ```

5. Retrieve a study through Connect to verify C-MOVE and the return C-STORE path, not just C-ECHO.

The existing data mounts and Caddy volumes are unchanged; do not delete them.

---

## ⚙️ Setting up Radiopaedia Connect

Radiopaedia Connect application settings are saved in its database, in `data/radiopaedia-connect/`. Configure them through the web UI or the API helper below; the upstream app does not import these settings from `appsettings.json`. The DICOM listener port is overridden at startup by `RCONNECT_SCP_PORT=4104` in Compose.

### Browser-free setup with Docker

The `connect-configure` service automatically applies [connect-config.json](./connect-config.json) through Connect's existing settings API on normal `docker compose up -d`. It uses the official application image unchanged and Python's standard library in a separate, read-only helper container. Create its required secret files before starting the stack.

1. Create a private directory and enter the admin password from a WSL/Linux Bash terminal. For an existing installation, use its **current** admin password. For a fresh installation, choose a new password of at least six characters.

   ```bash
   mkdir -p secrets/connect
   chmod 700 secrets secrets/connect
   (umask 077; read -r -s -p 'Connect admin password: ' password; printf '\n'; printf '%s' "$password" > secrets/connect/admin-password)
   ```

   The password is not echoed or placed in shell history. Everything under `secrets/` is git-ignored; do not commit credentials or paste them into Compose. Ordinary local files are not encrypted by Docker: protect this directory and the application database.

2. Review [connect-config.json](./connect-config.json). Its defaults configure `RCONNECT_SCP`, five concurrent downloads, and the `orthanc` node at `127.0.0.1:4242`, AE `ORTHANC`, calling AE `RCONNECT_SCP`.

3. Start the stack and apply the configuration:

   ```bash
   docker compose up -d
   docker compose logs -f connect-configure
   ```

   The helper waits up to 60 seconds for the API, creates/verifies the admin password, merges local settings, creates/updates Orthanc, restarts the DICOM listener, verifies saved values, and tests Connect → Orthanc with DICOM C-ECHO. Credentials and session cookies stay out of its logs and are held in memory. It connects directly to `http://127.0.0.1:5000` using the shared host network; Caddy is not required.

4. After changing configuration or secret files, run `docker compose up -d` again. Compose starts the exited helper again; successful completion leaves it in `Exited (0)` state, which is expected. Check its logs and exit status with `docker compose logs connect-configure` and `docker compose ps -a connect-configure`. Detached startup does not wait for configuration success: the app can remain running even if the helper fails.

The helper leaves unrelated settings/nodes alone and does not create duplicate Orthanc nodes on repeat runs. Each run briefly restarts the DICOM listener, even if database settings already match, to refresh its in-memory allowed calling AE titles after any earlier interrupted run. Apply while no DICOM transfers are active. A host/Docker reboot or `docker restart radiopaedia-connect` does **not** rerun the helper; saved database settings still persist. Likewise, `docker compose up -d radiopaedia-connect` targets only the app, not the helper. If a configuration run is already in progress, another `up` does not start a second one.

To apply settings explicitly and receive the helper's exit code in your terminal, use `docker compose run --rm connect-configure`. Do not run this concurrently with an automatic configuration run.

The default file does not change OAuth or SMTP credentials. If OAuth is not configured yet, the helper reports that setup is still incomplete, even when the requested DICOM settings were successfully applied.

#### Optional Radiopaedia credentials and SMTP

Create credential files using the same private input pattern:

```bash
(umask 077; read -r -s -p 'Radiopaedia client ID: ' value; printf '\n'; printf '%s' "$value" > secrets/connect/radiopaedia-client-id)
(umask 077; read -r -s -p 'Radiopaedia client secret: ' value; printf '\n'; printf '%s' "$value" > secrets/connect/radiopaedia-client-secret)
```

Add these properties inside `localSettings` in [connect-config.json](./connect-config.json), keeping its existing properties:

```json
"radiopaediaClientIdFile": "/run/secrets/radiopaedia-client-id",
"radiopaediaClientSecretFile": "/run/secrets/radiopaedia-client-secret"
```

The helper resolves `*File` properties before sending the request; they are not upstream API fields. Each secret file must contain one non-empty line. A trailing line ending is removed; other characters, including spaces, are preserved. For direct execution outside Docker, relative secret paths resolve against the configuration file's directory.

SMTP properties supported inside `localSettings` are `smtpHost`, `smtpPort`, `smtpUsername`, `smtpPasswordFile`, `smtpFromAddress`, and `notificationRecipients` (a comma-separated recipient string, matching the web UI). Store the SMTP password under `secrets/connect/` and reference its container path. `radiopaediaClientId` may also be supplied directly if it is not sensitive in your deployment.

Omitted local properties retain their current values. Empty strings explicitly clear string settings; `smtpPort: null` clears that port. The API masks existing OAuth/SMTP secrets as `********`; the helper preserves those masked values when merging. Read-back verifies only secret **presence**, not their actual contents. Explicitly supplied secrets are sent on every run because the API cannot reveal whether they already match.

Remote nodes are matched by `name`, ignoring case. Multiple matching nodes cause an error before settings writes. Keep the managed node's name stable: renaming it creates a new node and leaves the old one untouched. Omitting `orthancNode` leaves all nodes untouched and skips C-ECHO; omitting its `sortOrder` preserves the existing order (or uses zero for a new node).

**Failures return a nonzero exit code.** There is no cross-request transaction or rollback: earlier writes may already have persisted. Correct the problem and rerun. A failed echo means saved settings did not pass the connectivity test; inspect Orthanc's logs and AE/host/port settings. An incorrect existing password is never reset automatically.

To allow a slower startup, override the helper arguments:

```bash
docker compose run --rm connect-configure \
  --config /configure/connect.json \
  --admin-password-file /run/secrets/admin-password \
  --wait-seconds 120
```

The wait budget applies separately to API readiness and retrying failed DICOM echoes; individual requests have a 15-second timeout. Authentication/write failures are not retried. C-ECHO tests connectivity, not a full C-MOVE/C-STORE transfer. User authorisation on Radiopaedia remains a separate browser-based OAuth flow.

The helper targets the upstream settings API inspected at commit [`d074279`](https://github.com/radiopaedia/RadiopaediaConnect/tree/d074279a30c7ea65560f67152e287f0a0d6402df). Pin a compatible dated application image tag for predictable upgrades; `latest` can change this API. Test the helper itself without Docker or real credentials:

```bash
python3 -B -m unittest discover -s tests -v
```

### 1️⃣ Initial setup: admin password

On first launch you'll see **Initial Setup**. Choose an **Admin Password** (at least 6 characters). This password protects the **Settings** page.

### 2️⃣ Settings → Radiopaedia

| Field | Value |
|---|---|
| **Client ID** | Your Radiopaedia OAuth client ID |
| **Client Secret** | Your Radiopaedia OAuth client secret |

These are required for signing in and submitting cases. Get them from Radiopaedia.

> 🔐 Never commit these to git. They're stored only in the database under `data/`, which is git-ignored.

### 3️⃣ Settings → DICOM SCP

This is the DICOM receiver Radiopaedia Connect uses to accept images that Orthanc sends back.

| Field | Value | Why |
|---|---|---|
| **AE Title** | **`RCONNECT_SCP`** | Must match the AE title in Orthanc's `RCONNECT_SCP` modality entry in `docker-compose.yml`. Case-sensitive. |
| **Port** | `4104` | Set at startup by `RCONNECT_SCP_PORT`, not a Docker port mapping. |

### 4️⃣ Settings → Remote Nodes

Click **+ Add Node** and enter:

| Field | Value |
|---|---|
| **Display Name** | `orthanc` |
| **AE Title** | **`ORTHANC`** |
| **Host / IP** | **`127.0.0.1`** (shared host network) |
| **Port** | **`4242`** |
| **Calling AE Title** | **`RCONNECT_SCP`** |

Save it, then press the **DICOM C-ECHO (ping)** button on the node. A success means Radiopaedia Connect can reach Orthanc. ✅

### 5️⃣ Optional settings

| Section | What it's for |
|---|---|
| **Notifications** | SMTP settings and recipients for emails when jobs or the pipeline fail. SSL is turned on automatically for ports 587 and 465. |
| **Anonymisation** | Controls which DICOM tags are kept when anonymising. |
| **Logs** | App, pipeline and DICOM events, kept for 30 days. Your first stop when something fails. |
| **Change Password** | Change the admin password. |

### 🔁 How the AE titles fit together

```
radiopaedia-connect ──(calling AE: RCONNECT_SCP)──▶ ORTHANC @ 127.0.0.1:4242
        ▲                                              │
        │       C-MOVE destination: RCONNECT_SCP       │
        └──────────── 127.0.0.1:4104 ◀─────────────────┘
```

When Radiopaedia Connect asks Orthanc for a series, Orthanc looks up **`RCONNECT_SCP`** in its modality list (the `RCONNECT_SCP` entry in `docker-compose.yml`) and sends the images to `127.0.0.1:4104`. If you change an AE title in one place, change it in the other too.

---

## 🗄️ Orthanc configuration

Orthanc uses the official [`orthancteam/orthanc`](https://hub.docker.com/r/orthancteam/orthanc) image and is configured in two places.

### Shared settings: `docker-compose.yml`

These are set with `ORTHANC__*` environment variables. Each name maps to an Orthanc config key, e.g. `ORTHANC__DICOM_ALWAYS_ALLOW_ECHO` → `DicomAlwaysAllowEcho`.

```yaml
ORTHANC__DICOM_MODALITIES: |
  { "RCONNECT_SCP": ["RCONNECT_SCP", "127.0.0.1", 4104] }
```

### Machine-local settings: `orthanc.d/*.json` (git-ignored)

Any `*.json` file in `orthanc.d/` is merged with the settings above at startup. This is the place for things that only apply to your machine, such as your own viewer:

```json
{
  "DicomModalities": {
    "timiv": ["timiv", "127.0.0.1", 5035]
  }
}
```

Apply changes with:

```bash
docker compose up -d --force-recreate orthanc
```

> 💡 With host networking, use `127.0.0.1` for a service on the same host, or the remote machine's reachable IP for a service elsewhere. Old Docker bridge gateway addresses are no longer needed.

### 📤 Exporting files from Orthanc

`RestApiWriteToFileSystemEnabled` is on, so you can write an instance to disk:

```bash
curl -X POST http://localhost:8042/instances/<id>/export -d '/export/my-file.dcm'
```

Files appear in **`data/orthanc-export/`** on the host. Paths outside `/export` will fail, because the container is read-only.

---

## 💾 Where your data lives

All persistent data is in host folders. The containers themselves are **read-only** and can be deleted and recreated without losing anything.

```
rp-connect/
├── docker-compose.yml          # the whole stack, including the Caddyfile
├── orthanc.d/                  # 🔒 machine-local Orthanc config (git-ignored)
│   └── local.json
└── data/                       # 🔒 all persistent data (git-ignored)
    ├── radiopaedia-connect/    # settings DB, downloaded DICOMs, encryption keys
    ├── orthanc/                # Orthanc index + stored studies
    ├── orthanc-export/         # files written by the /export API
    └── orthanc-hostid          # fixed host ID used when generating DICOM UIDs
```

Caddy's certificates live in the Docker volumes `caddy_data` and `caddy_config`. Keeping them there means your browser keeps trusting the same local certificate authority.

### 🧳 Backing up

```bash
docker compose stop
tar czf rp-connect-backup-$(date +%F).tgz data orthanc.d
docker compose start
```

> ⚠️ `data/radiopaedia-connect/keys/` holds the keys that encrypt saved credentials. Always back it up together with the database.

---

## 🛠️ Everyday commands

| Task | Command |
|---|---|
| Start / update after editing the yml | `docker compose up -d` |
| Apply settings automatically with stack startup | `docker compose up -d` |
| Apply Connect settings explicitly (returns configuration exit code) | `docker compose run --rm connect-configure` |
| Stop | `docker compose stop` |
| Follow Orthanc logs (verbose) | `docker logs -f orthanc` |
| Follow Radiopaedia Connect logs | `docker logs -f radiopaedia-connect` |
| Test Orthanc → Radiopaedia Connect | `curl -X POST http://localhost:8042/modalities/RCONNECT_SCP/echo -d '{}'` |
| Upgrade Orthanc | Change the `orthancteam/orthanc:<version>` tag, then `docker compose up -d` |
| Pull a newer Radiopaedia Connect | `docker compose pull radiopaedia-connect && docker compose up -d` |

---

## 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| Browser warns about the certificate at `https://localhost:8443` | Expected: Caddy issues its own local certificate. Accept it, or trust Caddy's root CA. |
| C-ECHO from Radiopaedia Connect to Orthanc fails | Use host **`127.0.0.1`**, port **`4242`** (not `8042`), and AE **`ORTHANC`**. Update the existing node in Settings after migrating from bridge networking. |
| Studies are requested but never arrive in Radiopaedia Connect | The **DICOM SCP AE Title** must be exactly `RCONNECT_SCP`, matching Orthanc's modality entry. Check **Settings → Logs**. |
| Orthanc keeps restarting | Run `docker logs orthanc`. Make sure `data/orthanc-hostid` is a **file**, not a folder. |
| A `timiv` (or other local) modality is missing | Check that `orthanc.d/` exists, holds valid JSON and is mounted. Then recreate Orthanc. |
