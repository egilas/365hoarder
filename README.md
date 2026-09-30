# 365hoarder

SharePoint Online and OneDrive enumerator for Microsoft 365 assessments. `365enum.sh` discovers sites, libraries and files visible to the supplied identity. `365dl.sh` provides an `fzf` interface for searching and selectively downloading the resulting inventory.

Doesn't modify anything. Same search interface as [smbhoarder](https://github.com/egilas/smbhoarder).

## Coverage

- SharePoint sites returned by Microsoft Graph site search
- The tenant root site and its document libraries
- The signed-in user's OneDrive
- Paginated drive contents from the Microsoft Graph delta API

Teams channel files are included when their backing SharePoint site is discoverable.

## Installation - venv

Set up the Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
sudo apt install fzf
```

Activate the environment again in each new shell before running the tools.

## Installation - OS

If you are bold, you can drop the venv and try to use your OS' packages:

```bash
sudo apt install python3-pypdf python3-requests python3-termcolor fzf
``` 

## Authentication

365hoarder does not run `az login` or perform an interactive MSAL login. Use one of these authentication sources:

1. A FOCI refresh token in `creds.ini` (recommended for long-running jobs)
2. A Microsoft Graph access token in `M365AT` env var

### FOCI refresh token

[TokenTacticsV2](https://github.com/f-bader/TokenTacticsV2) can be used during an assessment to obtain and inspect FOCI refresh tokens. Store the refresh token and its client ID in `creds.ini`:

```ini
[DEFAULT]
REFRESHTOKEN=0.A...
CLIENT_ID=d3590ed6-52b3-4102-aeff-aad2292ab01c
DOMAIN=contoso.onmicrosoft.com
ACCESSTOKEN=
```

`DOMAIN` accepts either a tenant domain or tenant GUID. `CLIENT_ID` is set to Microsoft Office - this has per september 2026 the correct scope. The Graph scope is fixed to `https://graph.microsoft.com/.default`, so no separate scope setting is required.

```bash
chmod 600 creds.ini
./365enum.sh
```

`ACCESSTOKEN` may initially be empty. A valid cached token is reused; otherwise the refresh token is exchanged and the new access token. If refreshing fails, the tool stops and prints the error returned by Entra ID.

The token provider checks validity before requests and retries a Graph `401` after refreshing. This also allows a large download to continue beyond the lifetime of its initial access token. Automatic renewal is available only when using `creds.ini`.

Alternate files and sections are supported:

```bash
./365enum.sh -cfile engagement-creds.ini -s USER2
./365dl.sh -scan scan-YYYYMMDD-HHMMSS -cfile engagement-creds.ini -s USER2
```

### Access token from the browser

When Conditional Access makes scripted sign-in difficult, sign in normally through the browser and reuse a Microsoft Graph access token from that working session:

1. Open `https://office.com` or `https://contoso.sharepoint.com` and sign in.
2. Open Developer Tools, select **Network**, then reload or browse around.
3. Filter requests for `graph.microsoft.com`.
4. Open a Graph request and copy the value after `Authorization: Bearer`.
5. Confirm that the JWT `aud` is Microsoft Graph, `scp` includes `Sites.Read.All`, and `exp` is still in the future. `Files.Read.All` is useful for OneDrive and direct file downloads and should also be present.

Note: A token issued for SharePoint itself is not interchangeable with a Microsoft Graph token. Copy the Bearer token from a request whose destination is `graph.microsoft.com`.

Supply the raw token through the environment variable:

```bash
M365AT='ey...' ./365enum.sh

```

`M365AT` also works with `365dl.sh`. Static access tokens are short-lived (60 minutes usually) and cannot be renewed automatically.

## Enumeration

With `creds.ini` in the project directory:

```bash
./365enum.sh
```

Useful examples:

```bash
# Choose the output directory
./365enum.sh --output assessment-output

# Skip the legacy sharedWithMe pass
./365enum.sh --no-shared-items

# Change status frequency; use 0 to disable it
./365enum.sh --status-every 250

# Read statistics from the newest local scan
./365enum.sh --stats

# Read a particular complete or in-progress scan
./365enum.sh --stats scan-YYYYMMDD-HHMMSS
```

Run `./365enum.sh --help` for the complete option list.

## Output

Each scan directory contains:

- `sites.jsonl`: discovered sites
- `drives.jsonl`: discovered libraries and OneDrive
- `inventory.jsonl` and `inventory.csv`: file inventory
- `errors.jsonl`: API, access and extraction errors
- `summary.json`: scan settings and counters

Enumeration does not download files. 

## Interesting findings
Interesting findings can be generated separately after enumeration using the `365findings.sh` tools. This tool reads `inventory.jsonl`; it does not authenticate, download files or inspect file bodies.

```bash
./365findings.sh scan-YYYYMMDD-HHMMSS
```

This writes or replaces `findings.jsonl` in the scan directory. Built-in rules flag interesting filename terms and potentially sensitive extensions. Rules can be extended with `rules.example.json`, individual CLI values, or replaced entirely:

```bash
# Add rules from a JSON file
./365findings.sh scan-YYYYMMDD-HHMMSS --rules rules.example.json

# Add one-off terms and extensions
./365findings.sh scan-YYYYMMDD-HHMMSS \
  --term acquisition --term merger --extension config

# Use only explicitly supplied rules and choose the output path
./365findings.sh scan-YYYYMMDD-HHMMSS --no-defaults \
  --term confidential --output custom-findings.jsonl
```

Run `./365findings.sh --help` for all options. `./365enum.sh --stats SCAN_DIR`
includes the generated findings count when `findings.jsonl` exists.

## Search and download

```bash
./365dl.sh -scan scan-YYYYMMDD-HHMMSS -out dl
```

The picker mirrors the `smbhoarder` workflow:

- `ENTER`: download the current file and view extractable text
- `TAB`: select or deselect a row
- `CTRL-A`: toggle all rows
- `CTRL-SPACE`: download selected rows
- `CTRL-S` / `ALT-S`: site and library statistics
- `CTRL-F` / `ALT-F`: choose an extension and add it to the current search

The preview shows the number and combined size of selected files. When chunking is
active, select `[All files]` to search the entire catalog. Direct download is also
available with `-id 123` and optional `-view`.

## Final notes

- Graph throttling and transient server errors are retried with backoff.
- Set `NO_COLOR=1` to disable terminal colors.
- Large scans generate audit traffic and may take a long time. 
