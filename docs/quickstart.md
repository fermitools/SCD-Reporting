# Quickstart Guide

Get SCD Effort Reporting running locally in minutes.

- [Bootstrap script (fastest)](#bootstrap-script-fastest)
- [Manual setup — macOS](#manual-setup--macos)
- [Manual setup — Linux (Ubuntu / Debian)](#manual-setup--linux-ubuntu--debian)
- [Manual setup — Linux (RHEL / Fedora / Rocky)](#manual-setup--linux-rhel--fedora--rocky)
- [Manual setup — Windows](#manual-setup--windows)
- [Starting the server with AI Summary](#starting-the-server-with-ai-summary)
- [Next steps](#next-steps)

---

## Bootstrap script (fastest)

The repository ships with a bootstrap script that installs dependencies, creates the database, seeds initial data, and starts the server in a single command. It is idempotent — safe to run again after pulling new changes.

**Prerequisites:** Python 3.12+, Git. Node.js 20+ is optional (Tailwind falls back to the Play CDN in dev mode).

### macOS / Linux

```bash
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting
./bootstrap.sh --admin-password yourpassword
```

With AI Summary enabled:

```bash
./bootstrap.sh --admin-password yourpassword --anthropic-key sk-ant-...
```

### Windows (PowerShell)

```powershell
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting
.\bootstrap.ps1 -AdminPassword yourpassword
```

With AI Summary enabled:

```powershell
.\bootstrap.ps1 -AdminPassword yourpassword -AnthropicKey sk-ant-...
```

> If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` first.

### What the script does

1. Finds Python 3.12+ on your PATH
2. Creates a `.venv` virtual environment (skips if it already exists)
3. Installs Python dependencies from `requirements-dev.txt`
4. Installs Node dependencies for Tailwind CSS (skips gracefully if npm is absent)
5. Runs all database migrations
6. Seeds default projects and categories via `seed_taxonomy`
7. Creates the admin account via `seed_admin`
8. Starts the development server at `http://localhost:8000`

Log in with **scd-admin@fnal.gov** and the password you provided.

### Script options

| Option (bash) | Option (PowerShell) | Description |
|---|---|---|
| `--admin-password <pass>` | `-AdminPassword <pass>` | Admin account password. Can also be set via `SCD_INITIAL_ADMIN_PASSWORD` env var. |
| `--anthropic-key <key>` | `-AnthropicKey <key>` | Anthropic API key. Can also be set via `ANTHROPIC_API_KEY` env var. |
| `--no-server` | `-NoServer` | Run setup only; do not start the server. |
| `--skip-npm` | `-SkipNpm` | Skip Node/Tailwind dependency install. |

---

## Starting the server with AI Summary

The AI Summary feature on the Reports page calls the Anthropic API. Without a key the button returns an error in the UI; all other features work normally.

Obtain a key at [console.anthropic.com](https://console.anthropic.com), then pass it to the bootstrap script or export it before starting the server manually:

**macOS / Linux:**

```bash
# One-time with the bootstrap script
./bootstrap.sh --admin-password yourpassword --anthropic-key sk-ant-...

# Or export and start manually
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python manage.py runserver
```

**Windows (PowerShell):**

```powershell
# One-time with the bootstrap script
.\bootstrap.ps1 -AdminPassword yourpassword -AnthropicKey sk-ant-...

# Or set and start manually
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python manage.py runserver
```

**Changing the model** (optional):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_SUMMARY_MODEL=claude-haiku-4-5-20251001   # faster / cheaper
.venv/bin/python manage.py runserver
```

The default model is `claude-sonnet-5`. Admins can also edit the system prompt and user template from the Reports page without restarting the server.

**Using a LiteLLM proxy** (optional):

```bash
export ANTHROPIC_API_KEY=your-litellm-key
export ANTHROPIC_BASE_URL=https://litellm.example.org
export ANTHROPIC_SUMMARY_MODEL=azure/claude-sonnet-5
.venv/bin/python manage.py runserver
```

Leave `ANTHROPIC_BASE_URL` unset to use the standard Anthropic API endpoint.

---

## Manual setup — macOS

### 1. Install prerequisites

Install [Homebrew](https://brew.sh) if you do not already have it, then:

```bash
brew install python@3.12 node git
```

Verify:

```bash
python3.12 --version   # Python 3.12.x
node --version         # v20.x or later
git --version
```

### 2. Clone and set up

```bash
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements-dev.txt

cd theme/static_src && npm install && cd ../..
```

### 3. Initialise the database

```bash
python manage.py migrate
python manage.py seed_taxonomy
SCD_INITIAL_ADMIN_PASSWORD=yourpassword python manage.py seed_admin
```

### 4. Start the server

```bash
python manage.py runserver
```

Open <http://localhost:8000> and log in with `scd-admin@fnal.gov` / `yourpassword`.

> **Tailwind watcher (optional):** For CSS hot-reload during frontend work, open a second terminal and run `cd theme/static_src && npm start`. The dev settings load Tailwind from the Play CDN automatically so this is only needed when editing templates.

---

## Manual setup — Linux (Ubuntu / Debian)

Tested on Ubuntu 22.04 LTS and 24.04 LTS.

### 1. Install prerequisites

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev \
                   nodejs npm git build-essential
```

If `python3.12` is not in your distro's package manager, use the [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa):

```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

If Node is older than v18, install a current release via NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

### 2. Clone and set up

```bash
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements-dev.txt

cd theme/static_src && npm install && cd ../..
```

### 3. Initialise the database

```bash
python manage.py migrate
python manage.py seed_taxonomy
SCD_INITIAL_ADMIN_PASSWORD=yourpassword python manage.py seed_admin
```

### 4. Start the server

```bash
python manage.py runserver
```

Open <http://localhost:8000> and log in with `scd-admin@fnal.gov` / `yourpassword`.

---

## Manual setup — Linux (RHEL / Fedora / Rocky)

Tested on Rocky Linux 9 and Fedora 39.

### 1. Install prerequisites

```bash
sudo dnf install -y python3.12 python3.12-devel nodejs npm git gcc
```

On RHEL 8/9 you may need to enable the AppStream module first:

```bash
sudo dnf module enable python312
sudo dnf install -y python3.12 python3.12-devel
```

For Node.js 20+:

```bash
curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
sudo dnf install -y nodejs
```

### 2. Clone and set up

```bash
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements-dev.txt

cd theme/static_src && npm install && cd ../..
```

### 3. Initialise the database

```bash
python manage.py migrate
python manage.py seed_taxonomy
SCD_INITIAL_ADMIN_PASSWORD=yourpassword python manage.py seed_admin
```

### 4. Start the server

```bash
python manage.py runserver
```

Open <http://localhost:8000> and log in with `scd-admin@fnal.gov` / `yourpassword`.

---

## Manual setup — Windows

Two paths are available: native PowerShell or WSL 2. WSL 2 is recommended for a smoother experience.

### Option A — WSL 2 (recommended)

1. Install WSL 2 with Ubuntu (run PowerShell as Administrator):

   ```powershell
   wsl --install
   ```

   Restart when prompted, then open the Ubuntu terminal that appears.

2. Follow the [Linux (Ubuntu / Debian)](#manual-setup--linux-ubuntu--debian) instructions inside the WSL terminal. The app is available at <http://localhost:8000> from your Windows browser.

### Option B — Native Windows (PowerShell)

#### 1. Install prerequisites

- **Python 3.12** — download from <https://www.python.org/downloads/>. During install, check **"Add python.exe to PATH"**.
- **Node.js 20 LTS** — download from <https://nodejs.org/en/download>.
- **Git** — download from <https://git-scm.com/download/win>.

Open a new PowerShell window after installing so PATH changes take effect.

#### 2. Clone and set up

```powershell
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt

cd theme\static_src
npm install
cd ..\..
```

> If PowerShell blocks script execution: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

#### 3. Initialise the database

```powershell
python manage.py migrate
python manage.py seed_taxonomy
$env:SCD_INITIAL_ADMIN_PASSWORD = "yourpassword"
python manage.py seed_admin
```

#### 4. Start the server

```powershell
python manage.py runserver
```

Open <http://localhost:8000> and log in with `scd-admin@fnal.gov` / `yourpassword`.

---

## Next steps

| Task | Where |
|---|---|
| Add projects, categories, groups | `/taxonomy/` (Admin) |
| Invite or create other users | `/admin-users/` — use the Create user form or enable self-serve signup |
| Submit an effort entry | `/entries/new/` |
| Run a report or AI summary | `/reports/` (Admin / Auditor) |
| Configure the AI prompt | `/reports/` → AI Prompt Configuration panel (Admin) |
| View the audit log | `/audit/` (Admin / Auditor) |
| Deploy to production | [docs/deploy-docker.md](deploy-docker.md) |
