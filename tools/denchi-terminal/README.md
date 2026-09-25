# Denchi Terminal

`denchi-terminal` is a dedicated Raspberry Pi terminal for Denchi Card. It reads
the `lnurlw://` URI from a card through PaSoRi, retrieves card information from
LNbits, displays it on the console, and optionally prints an ESC/POS receipt.

It supports two explicit modes:

- `public`: looks up the public balance and usage history and prints the public
  receipt.
- `admin`: logs in to LNbits, maps Denchi Cards to their wallets and Withdraw
  links, retrieves a fresh balance at scan time, and prints an admin receipt.

## Hardware

- Raspberry Pi
- Sony PaSoRi or another NFC reader supported by nfcpy
- NFC tags containing a Denchi Card `lnurlw://` NDEF URL record
- Optional MUNBYN-compatible 80 mm ESC/POS network printer using raw TCP port
  9100
- LAN access from the Raspberry Pi to LNbits and, when used, the printer

## Python setup

From the Denchi repository root:

```bash
cd tools/denchi-terminal
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The Raspberry Pi may also need OS-level libusb packages and a udev rule that
allows the terminal user to access PaSoRi.

## Environment setup

Create a local configuration file:

```bash
cp .env.example .env
```

`.env` can contain credentials and is ignored by Git. Do not commit it. Command
line options continue to override their corresponding environment variables.

### Public mode

Public mode is the default. Configure at least:

```env
DENCHI_MODE=public
DENCHI_API_URL=https://your-lnbits-host/denchi/api/v1/public/lookup
```

To print receipts, also set `DENCHI_PRINTER_HOST`. The printer port defaults to
9100. `DENCHI_HISTORY_LIMIT` controls the number of usage entries printed and
must be between 0 and 20.

The equivalent command-line configuration is:

```bash
.venv/bin/python terminal.py \
  --api-url https://your-lnbits-host/denchi/api/v1/public/lookup \
  --printer-host your-printer-host \
  --printer-port 9100 \
  --history-limit 20
```

### Admin mode

Admin mode requires username/password authentication in LNbits:

```env
DENCHI_MODE=admin
DENCHI_BASE_URL=https://your-lnbits-host
DENCHI_USERNAME=your-username
DENCHI_PASSWORD=your-password
```

Set `DENCHI_PRINTER_HOST` as well to print admin receipts. Admin mode builds its
card lookup table at startup, so restart the terminal after issuing or changing
cards.

## Run

After configuring `.env`:

```bash
cd tools/denchi-terminal
.venv/bin/python terminal.py
```

Press Ctrl+C to stop. After processing a card, remove it from PaSoRi before
scanning another card.

## Troubleshooting

- **PaSoRi cannot be opened:** check its USB connection, libusb installation,
  and the current user's udev permissions.
- **LNbits login fails:** verify `DENCHI_BASE_URL`, the credentials, and that
  username/password login is enabled by the LNbits server.
- **API requests fail:** verify that the Raspberry Pi can reach the configured
  LNbits URL and that the Denchi and Withdraw extensions are enabled.
- **Card is not found:** confirm that its NDEF URL starts with `lnurlw://`. In
  admin mode, restart the terminal to rebuild the in-memory card table.
- **Printer connection fails:** check the printer host, TCP port, LAN routing,
  and that raw ESC/POS printing is enabled. Printing errors do not stop the
  terminal.
