# Denchi Card for LNbits

Denchi Card helps an LNbits account issue and manage NFC Lightning cards. One physical card corresponds to one LNbits wallet and one LNURL-withdraw link. Denchi Card coordinates those resources; it does not hold a separate balance or implement LNURL-withdraw itself. The extension ID is `denchi`.

The card carries a static `lnurlw://...` NDEF URI. Simple writable tags such as NTAG213 and NTAG215 can be used.

## Requirements

- Developed against LNbits **1.6.2** and the **Withdraw extension 1.3.0**. Withdraw must be installed, active, and accessible to the issuing account.
- An HTTPS page and a browser with Web NFC support are needed to write or scan tags. Android Chrome is the primary target. Do not assume desktop browsers or iPhone/Safari can perform these Web NFC operations.
- A compatible, empty or initialized writable NDEF tag for each card.
- A working LNbits funding source for actual Lightning withdrawals. Creating a Denchi Card does not fund its wallet.

## Installation

An LNbits administrator can install released versions through the extension manager:

1. Open **Manage Server > Server > Extensions Manifests**.
2. Add `https://raw.githubusercontent.com/GDur4n66/lnbits-extensions/main/manifest.json` and save.
3. Open **Manage Extensions > Add Remove Extensions**.
4. Find **Denchi Card**, select a release, and install it.
5. Install and activate the **Withdraw** extension separately, then enable Denchi Card for the issuing account.

The distribution catalog publishes pinned archives with SHA-256 checksums. For local development, place or symlink this repository as **`denchi`** inside the LNbits extensions directory. LNbits 1.6.2 uses `<LNBITS_EXTENSIONS_PATH>/extensions/` (by default, `lnbits/extensions/` relative to the directory from which LNbits runs). For a source checkout launched from its root, an example is:

```sh
ln -s /path/to/denchi /path/to/lnbits/lnbits/extensions/denchi
```

Restart LNbits so it discovers Denchi Card and runs its migrations. Open `/denchi/` while signed in. The public balance page is `/denchi/lookup` and does not require login. Ensure the extension directory is persistent in container deployments.

## Upgrading from Giftcard 0.1.0

Denchi Card 0.2.0 is a new extension ID, not an in-place update of `giftcard`.

1. Back up the LNbits data folder/database. Disable the old Giftcard extension
   before installing Denchi Card. Do not run both managers concurrently.
2. Install Denchi Card on the same LNbits instance/data folder. Its initial
   migrations copy legacy Profiles and card records into `ext_denchi` (SQLite)
   or the `denchi` schema (PostgreSQL). The old database is not deleted.
3. Enable Denchi Card for each issuing account. Check card counts, balances and
   NFC lookup before removing the old installation. Existing wallets, ownership,
   Withdraw links, lookup hashes and NFC-written flags are preserved.
4. Update bookmarks and integrations from `/giftcard/` to `/denchi/`. Management
   APIs now use `/denchi/api/v1/cards`; old API paths are not aliases. Update
   printer-terminal bookmarks too. Existing NFC tags need no rewrite: their
   `/withdraw/...` URLs stay unchanged.

Receipt settings now use `DENCHI_RECEIPT_*`; legacy `GIFTCARD_RECEIPT_*` names
remain accepted as fallbacks, with the new names taking precedence. Import runs
once; changes made later through the old extension will not be synchronized.
If Denchi Card was installed before the legacy database was made available,
restore the backup and plan migration before issuing new cards; do not merge
live databases manually. The GitHub repository is
[`GDur4n66/denchi`](https://github.com/GDur4n66/denchi).

## Profiles and issuing

Profiles are issue-time templates. Create, list, and delete them on the Denchi Card page; there is no edit action. To change settings, create a new Profile. Each Profile has a name, a wallet-name base, a positive maximum payment per use in sats, and optional expiration in whole **days**, **weeks**, or **months**. A month is treated as 30 days when the expiry is converted to seconds.

Operator workflow:

1. Create a Profile with a wallet-name base, maximum payment per use, and optional expiration.
2. Select it and click **Issue Card**. Denchi Card creates a wallet owned by your LNbits account and a matching Withdraw link. The wallet name is `<wallet-name-base>-<six-character-random-suffix>`, using uppercase letters and digits.
3. When prompted to **Tap an NFC card**, present an empty writable tag. The new Denchi Card starts **Unwritten** and becomes **Written** only after the browser reports a successful write and the status update succeeds.
4. Fund the new wallet through LNbits if the card needs a balance. Denchi Card has no funding or top-up action in its UI.
5. Give the written card to its recipient.

The Withdraw link is created in sats with a 1-sat minimum, the Profile's maximum per use, 250 uses, a one-second wait, and the Profile's expiration (or no expiration). Profiles are not copied into issued-card records.

## Writing NFC tags

Denchi Card takes the authoritative URL returned by Withdraw and changes only its scheme to `lnurlw://` for the tag. It writes one NDEF URI record with Web NFC, requesting **no overwrite** of existing NDEF content. Prepare an empty or initialized tag; do not use this flow to replace an existing tag's data.

If writing fails or you close the dialog before writing, the wallet, Withdraw link, and Denchi Card remain in place as **Unwritten**. Retry in the dialog or use **Write NFC** on that row later; this does not issue another card. After a successful write, the browser starts a separate, experimental two-second NFC scan to try to reduce Android's normal tag-handling popups. This does not guarantee that the system chooser will be suppressed.

## Managing Cards

The paginated Cards list shows the LNbits wallet name and balance, a link to Withdraw's public share page, Withdraw's current Enabled/Disabled state, NFC state, and creation time. Wallet-name search and Written/Unwritten filtering are available. Enabled-state filtering is not implemented.

Use **Enable** or **Disable** beside a row's status to change its Withdraw link; Denchi Card reads the state back from Withdraw. **Disable All Cards** requires confirmation and disables currently enabled cards owned by the signed-in account. There is no bulk Enable action; disabled cards can be enabled individually.

Deleting a Denchi Card requires confirmation showing the wallet name and current balance. The operation removes the Withdraw link, soft-deletes the LNbits wallet, then removes the Denchi Card record. A nonzero balance does not block deletion, and Denchi Card does not transfer or sweep remaining funds. Review the balance before confirming. If a step fails, the operation reports an error instead of claiming success.

## Public Balance Check

Open `/denchi/lookup` without signing in. The page attempts to start Web NFC scanning automatically; if the browser requires user activation, use **Start Scan**. It displays **Tap your card** while scanning. A scan shows the wallet's current balance in sats and up to 20 recent successful uses of that card's Withdraw link, with only date/time and amount. Scanning continues on the same page; tapping another card replaces the result. **Stop Scan** ends the session. This normal public page has no print controls and never auto-prints.

## Bulk top-up tool

[`tools/bulk_topup.py`](tools/bulk_topup.py) is a separate SuperUser maintenance script, **not** a Denchi Card UI feature. It uses LNbits' official SuperUser API to add a specified number of sats to matching wallets of **one specified account**. It ignores deleted and shared wallets and matches names beginning with `<wallet-prefix>-` (for example, `gc-ABC123`, but not `gc2-ABC123`).

The script requires Python 3.10+ and no third-party packages. It can run remotely; it does not require LNbits or Denchi Card to be installed on the operator's machine. Preview first:

```sh
python3 tools/bulk_topup.py \
  --url https://lnbits.example.com \
  --user '<account-id>' \
  --wallet-prefix gc \
  --amount 10000 \
  --dry-run
```

If `LNBITS_SUPERUSER_ACCESS_TOKEN` is unset, the script prompts for a SuperUser username and a password that is not echoed. It verifies SuperUser access before looking up wallets. A dry run shows the matches, current balances, amount per wallet, and total without sending any top-up requests. Remove `--dry-run` for a real run: the script warns that the operation is additive and requires typing `YES`, unless `--yes` is supplied. **Running it twice adds the amount twice.**

## Security and architecture

The NFC URI is static, not proof that someone holds the physical card. NTAG213/215 tags do not provide cryptographic card authentication here. Anyone who copies the URI can make the same public balance/history request; this extension does not prevent cloning. The public response contains only the balance and the latest usage timestamps and amounts, not wallet keys or payment details.

Denchi Card's database holds Profiles and card relationships/management metadata, not a second ledger. LNbits wallets remain authoritative for balances and payments; Withdraw remains authoritative for LNURL-withdraw links and enabled state. New cards store a SHA-256 lookup index derived from Withdraw's unique identifier so public lookup can find the card without storing the raw identifier as a lookup field. Denchi Card does not duplicate balances, payment history, or Withdraw enabled state.

## Optional receipt printing

Receipt printing is included in `main` but is optional. Normal Denchi Card use needs no printer or printer configuration. It is intended for a dedicated event/operator terminal, not the ordinary public Balance Check page.

Configure these values in LNbits' `.env` when starting from its source directory with `uv run lnbits`, or pass them as process/container environment variables:

- `DENCHI_RECEIPT_PRINTER`: a LAN printer URL such as `tcp://printer.local:9100`. It must use `tcp://` with a host and explicit port; an unset or invalid value disables printing.
- `DENCHI_RECEIPT_TERMINAL_TOKEN`: a secret of 32–256 URL-safe letters, digits, `_`, or `-`. Both a valid token and printer URL are required for the printer-terminal page.
- `DENCHI_RECEIPT_AUTO_PRINT`: optional. `1`, `true`, `yes`, or `on` (case-insensitive, surrounding whitespace ignored) enables automatic printing after a successful lookup on the printer terminal. Any other or unset value leaves manual printing only.

Configuration is read once per LNbits process; restart LNbits after changing it. Open `/denchi/lookup/print/<token>` on the dedicated terminal, replacing `<token>` with the configured secret. An invalid token or printer configuration returns 404. After a card lookup, **Print Receipt** sends the current balance and up to 20 usage entries to the server-side LAN ESC/POS printer over TCP. If auto-print is enabled, successful scans also request a receipt; repeated auto-prints for the same card are suppressed for five seconds. Manual printing remains available, and printer failures do not stop balance lookup or NFC scanning.

Keep the terminal URL/token private: it authorizes printing without an LNbits login. The printer address is never supplied by the browser, and the normal `/denchi/lookup` page neither shows a print button nor triggers printing.

## Development status and license

Denchi Card is actively developed and covers the current manual issue, write, manage, and lookup workflow. It is not an official LNbits extension or a stable 1.0 release. The repository includes an [MIT license](LICENSE).
