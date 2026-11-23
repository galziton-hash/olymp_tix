"""
Olympiacos - Milan  -  Ticket Availability Checker
===================================================
Event page:
  https://olympiacos.more.com/gr-en/tickets/sports/pae-olympiakos-eisitiria-agonon-2026-1/
  Match: OLYMPIACOS - MILAN, Thu 26/11 19:45

How the site works (confirmed via live browser inspection 2026-11-18):

  Price list (public, no login):
    GET https://olympiacos.more.com/_api/events/<EVENT_ID>
    -> "pricelist": zones (VIP, VVIP, GATE n ...), each with "sections"
       (the gates on the stadium map, e.g. "VVIP C") and their prices
       in "discounts" (first one = regular ticket).
    A zone that is grey on the map has no sections / priceRange "-".
    NOTE: "availableSeats" is always 0 for numbered seats - do not trust it.

  Seat map (after clicking "Tickets" on the match):
    <area data-section-name="VVIP C" ...>   -> click opens the gate's seats
    <li class="... sa">        free seat (orange)
    <li class="... seat-sd">   taken seat (grey)
    data-row="Σ12" data-seat="Θ5"  (Σ = row, Θ = seat)
    #continue -> SeatSelector.reserveSeats() -> seats go to the cart.

  maxTicketsPerOrder for this match is 2.

Flow per run:
  1. Pre-check the public price list for a gate whose regular price < MAX_PRICE.
     (With --precheck only this step runs; used by GitHub Actions so the
      browser is installed only when something is found.)
  2. Log in, open the match, click that gate on the map.
  3. If the gate has >= TICKETS free seats (adjacent preferred): select them
     and press Continue (adds them to the cart).
  4. Send NTFY push + email.

Environment variables (GitHub Secrets):
  LOGIN_EMAIL        - more.com account e-mail
  LOGIN_PASSWORD     - more.com account password
  NTFY_TOPIC         - ntfy.sh topic
  NTFY_SERVER        - ntfy server base URL      (default: https://ntfy.sh)
  EMAIL_SENDER       - Gmail address that sends alerts (needs App Password)
  EMAIL_APP_PASSWORD - Gmail App Password
  EMAIL_RECIPIENTS   - comma-separated recipients
Optional:
  EVENT_ID   (default: Olympiacos - Milan)
  MAX_PRICE  (default: 300)  - per-ticket price must be BELOW this
  TICKETS    (default: 2)
  DRY_RUN    (default: 0)    - 1 = select seats but do NOT press Continue
"""

import asyncio
import io
import os
import re
import smtplib
import ssl
import sys
import traceback
import unicodedata
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

# ── UTF-8 output ───────────────────────────────────────────────────────────────
if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_URL   = "https://olympiacos.more.com"
TARGET_URL = f"{BASE_URL}/gr-en/tickets/sports/pae-olympiakos-eisitiria-agonon-2026-1/"
LOGIN_URL  = (f"{BASE_URL}/Challenge.aspx?origin="
              "https%3a%2f%2folympiacos.more.com%2fgr-en%2ftickets%2fsports%2f"
              "pae-olympiakos-eisitiria-agonon-2026-1%2f&terms=olympiacos&lang=en")

EVENT_ID  = os.environ.get("EVENT_ID") or "8e6e47a4-24bf-4e19-a2d5-f329b34a568f"
MAX_PRICE = float(os.environ.get("MAX_PRICE") or 300)
TICKETS   = int(os.environ.get("TICKETS") or 2)
DRY_RUN   = (os.environ.get("DRY_RUN") or "0") == "1"
SALE_WAIT_SECONDS = int(os.environ.get("SALE_WAIT_SECONDS") or 420)
QUEUE_WAIT_SECONDS = int(os.environ.get("QUEUE_WAIT_SECONDS") or 1500)

EMAIL    = os.environ.get("LOGIN_EMAIL",    "")
PASSWORD = os.environ.get("LOGIN_PASSWORD", "")

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC",  "")
NTFY_SERVER = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"

EMAIL_SENDER       = os.environ.get("EMAIL_SENDER",       "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_RECIPIENTS   = [
    addr.strip()
    for addr in os.environ.get("EMAIL_RECIPIENTS", "").split(",")
    if addr.strip()
]

SS = "screenshots"
os.makedirs(SS, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────────
def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_GREEK_DIGRAPHS = [("ΟΥ", "OU"), ("ΑΥ", "AV"), ("ΕΥ", "EV"), ("ΓΓ", "NG"),
                   ("ΓΚ", "GK"), ("ΜΠ", "B"), ("ΝΤ", "NT"), ("ΤΣ", "TS"), ("ΤΖ", "TZ")]
_GREEK_LETTERS = dict(zip("ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ",
                          ["A", "V", "G", "D", "E", "Z", "I", "TH", "I", "K", "L", "M",
                           "N", "X", "O", "P", "R", "S", "T", "Y", "F", "CH", "PS", "O"]))


def to_english(text: str) -> str:
    """Transliterates Greek names (the site has no English version for some events)."""
    text = unicodedata.normalize("NFD", text.upper())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").replace("ς", "Σ")
    text = text.replace("ΘΥΡΑ", "GATE").replace("ΟΛΥΜΠΙΑΚΟΣ", "OLYMPIACOS")
    for gr, en in _GREEK_DIGRAPHS:
        text = text.replace(gr, en)
    return "".join(_GREEK_LETTERS.get(c, c) for c in text)


async def send_ntfy(title: str, body: str, priority: str = "urgent",
                    click: str | None = None) -> None:
    if not NTFY_TOPIC:
        print("[NTFY] Skipped - NTFY_TOPIC not set.")
        return
    priority_map = {"max": 5, "urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}
    payload = {
        "topic":    NTFY_TOPIC,
        "title":    title,
        "message":  body,
        "priority": priority_map.get(priority, 5),
        "tags":     ["ticket", "soccer", "tada"],
    }
    if click:
        payload["click"] = click
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(NTFY_SERVER, json=payload)
            resp.raise_for_status()
        print(f"[NTFY] Sent -> {NTFY_SERVER}/{NTFY_TOPIC}")
    except Exception as exc:
        print(f"[NTFY] FAILED: {exc}")


def send_email(subject: str, body: str) -> None:
    if not EMAIL_SENDER or not EMAIL_APP_PASSWORD or not EMAIL_RECIPIENTS:
        print("[Email] Skipped - EMAIL_SENDER / EMAIL_APP_PASSWORD / EMAIL_RECIPIENTS not set.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Olympiacos Ticket Checker <{EMAIL_SENDER}>"
        msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        html = (
            "<html><body><pre style='font-family:monospace;font-size:14px;'>"
            + body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</pre></body></html>"
        )
        msg.attach(MIMEText(html, "html", "utf-8"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls(context=ctx)
            s.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        print(f"[Email] Sent to: {', '.join(EMAIL_RECIPIENTS)}")
    except Exception as exc:
        print(f"[Email] FAILED: {exc}")


# ── 1. Pre-check (public price list) ───────────────────────────────────────────
def find_candidate_gates() -> tuple[str, list[dict]]:
    """
    Returns (event_name, gates) where gates are the coloured gates whose
    regular ticket price is below MAX_PRICE, cheapest first:
      { zone, section, price }
    """
    resp = httpx.get(f"{BASE_URL}/_api/events/{EVENT_ID}", timeout=30,
                     headers={"Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()

    print(f"[precheck] Event: {data.get('name')}  "
          f"(max tickets per order: {data.get('maxTicketsPerOrder')})")
    gates = []
    for zone in data.get("pricelist", []):
        sections = zone.get("sections") or []
        print(f"  zone {zone.get('name')!r:10} price={zone.get('priceRange')!r:12} "
              f"gates={[s.get('name') for s in sections]}")
        for sec in sections:
            discounts = [d for d in sec.get("discounts") or [] if not d.get("soldout")]
            if not discounts:
                continue
            price = float(discounts[0]["price"])
            if price < MAX_PRICE:
                zone_name = to_english(zone.get("name") or "")
                label = zone_name if len(sections) == 1 else to_english(sec.get("name") or "")
                gates.append({"zone": zone_name, "section": sec.get("name"),
                              "label": label, "price": price})
    gates.sort(key=lambda g: g["price"])
    return to_english(data.get("name") or "event"), gates


async def goto_through_queue(page, url: str) -> None:
    """
    During busy sales the site sends visitors through a Queue-it waiting room
    (*.queue-it.net) that redirects back when it is our turn.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=QUEUE_WAIT_SECONDS * 1000)
    except Exception:
        if "queue-it" not in page.url:
            raise
    if "queue-it" in page.url:
        print(f"[queue] In the waiting room: {page.url[:80]}")
        await page.wait_for_url(lambda u: "queue-it" not in u,
                                timeout=QUEUE_WAIT_SECONDS * 1000)
        await page.wait_for_load_state("domcontentloaded")
        print(f"[queue] Through the waiting room -> {page.url[:80]}")


# ── 2. Login ───────────────────────────────────────────────────────────────────
async def accept_cookies(page) -> None:
    try:
        btn = page.locator("a:has-text('ACCEPT ALL')").first
        if await btn.is_visible(timeout=3_000):
            await btn.click()
            print("[cookies] Accepted.")
    except Exception:
        pass


async def do_login(page) -> bool:
    print(f"[{now()}] Opening login page...")
    await goto_through_queue(page, LOGIN_URL)
    await accept_cookies(page)

    try:
        await page.wait_for_selector("#username", state="visible", timeout=30_000)
    except Exception:
        await page.screenshot(path=f"{SS}/01_login_no_form.png")
        print(f"[login] FAIL: email field not found. URL={page.url}")
        return False

    await page.fill("#username", EMAIL)
    await page.click("#init_Continue")

    try:
        await page.wait_for_selector("#PasswordSignup", state="visible", timeout=20_000)
    except Exception:
        await page.screenshot(path=f"{SS}/02_login_no_password.png")
        print("[login] FAIL: password step did not appear "
              "(account not recognised?).")
        return False

    await page.fill("#PasswordSignup", PASSWORD)
    await page.click("#sin_btn")

    try:
        await page.wait_for_url(re.compile(r"^https://olympiacos\.more\.com/"), timeout=45_000)
    except Exception:
        await page.screenshot(path=f"{SS}/03_login_failed.png")
        print(f"[login] FAIL: still at {page.url}")
        return False

    print(f"[login] OK. URL={page.url}")
    return True


# ── 3. Select seats ────────────────────────────────────────────────────────────
PICK_SEATS_JS = """(n) => {
    const num = s => parseInt((s || '').replace(/\\D/g, ''), 10);
    const free = [...document.querySelectorAll('#map li.sa')].map(li => ({
        row: li.dataset.row, seat: li.dataset.seat, n: num(li.dataset.seat),
        d: li.dataset.d,
    }));
    const byRow = {};
    free.forEach(s => (byRow[s.row] = byRow[s.row] || []).push(s));
    // Prefer n consecutive seats in one row
    for (const row of Object.keys(byRow)) {
        const seats = byRow[row].sort((a, b) => a.n - b.n);
        for (let i = 0; i + n <= seats.length; i++) {
            const run = seats.slice(i, i + n);
            if (run[n - 1].n - run[0].n === n - 1) return {free: free.length, adjacent: true, pick: run};
        }
    }
    return {free: free.length, adjacent: false, pick: free.length >= n ? free.slice(0, n) : []};
}"""


async def open_gate(page, section_name: str) -> bool:
    opened = await page.evaluate("""(name) => {
        const a = [...document.querySelectorAll('area[data-section-name]')]
                  .find(a => a.dataset.sectionName === name);
        if (!a) return false;
        jQuery(a).trigger('click');
        return true;
    }""", section_name)
    if not opened:
        print(f"[seats] Gate {section_name!r} not found on the map.")
        return False
    try:
        await page.wait_for_selector("#map li[data-seat]", timeout=20_000)
    except Exception:
        print(f"[seats] Seats of gate {section_name!r} did not load.")
        return False
    await asyncio.sleep(1.5)
    return True


async def select_seats(page, gate: dict) -> dict | None:
    """Opens the gate and selects TICKETS seats. Returns details or None."""
    if not await open_gate(page, gate["section"]):
        return None

    result = await page.evaluate(PICK_SEATS_JS, TICKETS)
    print(f"[seats] {gate['label']}: {result['free']} free seat(s).")
    if len(result["pick"]) < TICKETS:
        await page.evaluate("SeatSelector.backToFullView()")
        await asyncio.sleep(2)
        return None

    for s in result["pick"]:
        await page.evaluate("""(d) => {
            const li = document.querySelector(`#map li.sa[data-d="${d}"]`);
            (li.querySelector('a') || li).click();
        }""", s["d"])
        await asyncio.sleep(0.7)

    selected = await page.evaluate("SeatSelector.map.getSelectedSeats().length")
    total_txt = await page.locator("span[scope='tickets-total-amount']").first.inner_text()
    total = float(re.sub(r"[^\d.,]", "", total_txt).replace(",", ".") or 0)
    seats_str = ", ".join(f"Row {s['row'].lstrip('Σ')} Seat {s['seat'].lstrip('Θ')}"
                          for s in result["pick"])
    print(f"[seats] Selected {selected}: {seats_str}  total={total_txt}")
    await page.screenshot(path=f"{SS}/05_seats_selected.png")

    if selected != TICKETS:
        print("[seats] Selection count mismatch - aborting.")
        return None
    if total >= MAX_PRICE * TICKETS:
        print(f"[seats] Total {total} is not below {MAX_PRICE * TICKETS} - aborting.")
        return None

    return {"gate": gate, "seats": seats_str, "adjacent": result["adjacent"],
            "total": total_txt}


async def add_to_cart(page) -> tuple[bool, str]:
    start_url = page.url
    await page.click("#continue")
    print("[cart] Clicked Continue.")
    try:
        await page.wait_for_url(lambda u: u != start_url, timeout=45_000)
    except Exception:
        pass
    await asyncio.sleep(4)
    await page.screenshot(path=f"{SS}/06_after_continue.png", full_page=True)
    print(f"[cart] URL after Continue: {page.url}")
    return page.url != start_url, page.url


# ── Main ───────────────────────────────────────────────────────────────────────
async def run_browser(event_name: str, gates: list[dict]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            if not await do_login(page):
                msg = f"Login to more.com failed at {now()}."
                await send_ntfy("Olympiacos Checker - Login Failed", msg, priority="high")
                send_email("Olympiacos Checker - Login Failed", msg)
                sys.exit(1)

            print(f"\n[{now()}] Opening match seat map...")
            btn = page.locator(f"a[eventityid='{EVENT_ID}']").first
            deadline = asyncio.get_running_loop().time() + SALE_WAIT_SECONDS
            while True:
                await goto_through_queue(page, TARGET_URL)
                await accept_cookies(page)
                try:
                    await btn.wait_for(state="attached", timeout=10_000)
                    break
                except Exception:
                    pass
                # Before the sale opens the button is disabled with a popover
                # like "Η προπώληση θα ξεκινήσει στις 12:00" and has no eventityid.
                note = await page.evaluate("""() => {
                    const b = document.querySelector('a.eb-button--disabled[data-content]');
                    return b ? b.getAttribute('data-content') : null;
                }""")
                if asyncio.get_running_loop().time() > deadline:
                    print(f"[main] Tickets button still closed ({note}) - giving up this run.")
                    return
                print(f"[main] Tickets button not open yet ({note}) - reloading...")
                await asyncio.sleep(5)
            await btn.evaluate("b => { b.scrollIntoView({block: 'center'}); b.click(); }")
            await page.wait_for_selector("area[data-section-name]", state="attached",
                                         timeout=45_000)
            await asyncio.sleep(2)
            await page.screenshot(path=f"{SS}/04_seat_map.png")

            picked = None
            for gate in gates:
                picked = await select_seats(page, gate)
                if picked:
                    break

            if not picked:
                print(f"\n  Price list shows gates under {MAX_PRICE:.0f}€ but no gate has "
                      f"{TICKETS} free seats right now - done.")
                return

            if DRY_RUN:
                added, cart_url = False, page.url
                status = "DRY RUN - seats selected, Continue NOT pressed"
            else:
                added, cart_url = await add_to_cart(page)
                status = ("ADDED TO CART" if added
                          else "SEATS SELECTED (Continue may have failed - check the site)")

            gate = picked["gate"]
            title = f"{event_name}: {TICKETS} tickets {gate['label']} {gate['price']:.0f}€!"
            body = (
                f"Status: {status}\n\n"
                f"Match: {event_name}\n"
                f"Gate: {gate['label']} (zone {gate['zone']}, section {gate['section']})\n"
                f"Price per ticket: {gate['price']:.0f}€\n"
                f"Seats: {picked['seats']}"
                f"{' (side by side)' if picked['adjacent'] else ' (not side by side)'}\n"
                f"Total: {picked['total']}\n"
                f"Found at: {now()}\n\n"
                f"The reservation expires after ~10 minutes. Log in and complete it:\n"
                f"{cart_url}\n"
                f"Account: {EMAIL}"
            )
            print(f"\n{'='*60}\n{title}\n{body}\n{'='*60}")
            await send_ntfy(title, body, priority="max", click=cart_url)
            send_email(title, body)

        except SystemExit:
            raise
        except Exception:
            tb = traceback.format_exc()
            print(f"[main] UNHANDLED ERROR:\n{tb}")
            try:
                await page.screenshot(path=f"{SS}/error.png")
            except Exception:
                pass
            err_subject = "Olympiacos Checker - Unexpected Error"
            err_body    = f"{now()}\n\n{tb[:1500]}"
            await send_ntfy(err_subject, err_body, priority="high")
            send_email(err_subject, err_body)
            sys.exit(2)
        finally:
            await browser.close()


def main() -> None:
    precheck_only = "--precheck" in sys.argv
    print(f"\n{'='*60}")
    print(f"  Olympiacos Ticket Checker - {now()}")
    print(f"  Event {EVENT_ID}  |  {TICKETS} tickets  |  price < {MAX_PRICE:.0f}€"
          f"{'  |  DRY RUN' if DRY_RUN else ''}")
    print(f"{'='*60}\n")

    event_name, gates = find_candidate_gates()
    found = bool(gates)
    if found:
        print(f"\n[precheck] Gates under {MAX_PRICE:.0f}€: "
              + ", ".join(f"{g['label']} ({g['price']:.0f}€)" for g in gates))
    else:
        print(f"\n[precheck] No gate under {MAX_PRICE:.0f}€ - done.")

    if precheck_only:
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"found={'true' if found else 'false'}\n")
        return

    if found:
        asyncio.run(run_browser(event_name, gates))

    print(f"\n=== Run complete at {now()} ===\n")


if __name__ == "__main__":
    main()
