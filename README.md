# Event Seat Monitor

Automated availability checker for a specific football match.
Watches the event price list and, when a gate drops below the price limit,
adds the seats to the cart and sends a push notification + email.

---

## How It Works

```
Runs every 5 minutes (cron-job.org -> GitHub Actions workflow_dispatch)
  │
  ├─ 1. Read the public price list of the event (no login, a few seconds)
  ├─ 2. Any gate with a regular price below MAX_PRICE?  No -> done
  ├─ 3. Log in, open the match, click that gate on the stadium map
  ├─ 4. If it has TICKETS free seats (side by side preferred) -> select them
  ├─ 5. Press Continue -> seats are added to the cart
  └─ 6. Send push notification (NTFY) + email alert
```

---

## Setup

### GitHub Secrets required

Configure these in **Settings → Secrets and variables → Actions → Secrets**:

| Secret name          | Description                          |
|----------------------|--------------------------------------|
| `LOGIN_EMAIL`        | Ticketing site account email         |
| `LOGIN_PASSWORD`     | Ticketing site account password      |
| `NTFY_TOPIC`         | Your private NTFY topic name         |
| `EMAIL_SENDER`       | Gmail address for sending alerts     |
| `EMAIL_APP_PASSWORD` | Gmail App Password                   |
| `EMAIL_RECIPIENTS`   | Comma-separated alert recipients     |

### Optional variables (Settings → Variables)

| Variable name  | Default           |
|----------------|-------------------|
| `NTFY_SERVER`  | `https://ntfy.sh` |
| `MAX_PRICE`    | `300`             |
| `TICKETS`      | `2`               |

### Trigger

Create a job on [cron-job.org](https://cron-job.org) every 5 minutes:

- URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/check_tickets.yml/dispatches`
- Method: `POST`
- Headers: `Authorization: Bearer <GitHub token with Actions write>`,
  `Accept: application/vnd.github+json`
- Body: `{"ref":"main"}`

### Local test

```
pip install -r requirements.txt
python -m playwright install chromium
set LOGIN_EMAIL=...  &  set LOGIN_PASSWORD=...
set DRY_RUN=1            (select seats but do not press Continue)
python check_tickets.py
```

---

## Notifications

Uses [NTFY](https://ntfy.sh) for push notifications.
Install the NTFY app and subscribe to your private topic to receive alerts instantly.
