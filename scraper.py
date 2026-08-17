from playwright.sync_api import sync_playwright, TimeoutError
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import random
import urllib.parse
import sys
import os
import json
import phonenumbers
import signal
import re
from datetime import datetime
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


# ---- CONFIG ----
HEADLESS_MODE = True
SERVICE_ACCOUNT_FILE = "service_account.json"
MASTER_SHEET_NAME = "India Data"
RETRY_LIMIT = 3
PLAYWRIGHT_TIMEOUT = 15000
PROGRESS_FILE = "scraper_progress.json"
MAX_THREADS = 5

KNOWN_CHAINS = [
    "clove dental",
    "sabka dentist",
    "apollo dental",
    "toothsi",
    "32 pearls",
    "mydentist",
    "dr. batra"
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0"
]

console = Console()
data_lock = threading.RLock()
google_sheets_lock = threading.Lock()
progress_lock = threading.Lock()

active_workers = {}
latest_log = "Initializing Engine..."
gspread_client = None


def update_log(msg):
    global latest_log

    timestamp = datetime.now().strftime("%H:%M:%S")

    with data_lock:
        latest_log = f"[{timestamp}] {msg}"


def clean_address(address_str):
    if not address_str:
        return ""
    cleaned = re.sub(r"^Address:\s*", "", address_str, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    return cleaned.strip()


def is_chain_listing(business_name):
    name_lower = business_name.lower()
    return "Yes" if any(chain in name_lower for chain in KNOWN_CHAINS) else "No"


def make_fingerprint(name, phone, location):
    clean_name = re.sub(r"\s+", " ", name).strip().casefold()
    clean_loc = re.sub(r"\s+", " ", location).strip().casefold()

    try:
        parsed_phone = phonenumbers.parse(phone, "IN")
        clean_phone = phonenumbers.format_number(
            parsed_phone,
            phonenumbers.PhoneNumberFormat.E164
        )
    except Exception:
        clean_phone = re.sub(r"\D", "", phone)

    return f"{clean_name}|{clean_phone}|{clean_loc}"


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()

    return set()


def save_completed_task(task_id):
    with progress_lock:
        completed = load_progress()
        completed.add(task_id)

        with open(PROGRESS_FILE, "w") as f:
            json.dump(list(completed), f, indent=4)


def get_sheets_client():
    global gspread_client

    with google_sheets_lock:
        if gspread_client is None:
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]

            creds = ServiceAccountCredentials.from_json_keyfile_name(
                SERVICE_ACCOUNT_FILE,
                scope
            )

            gspread_client = gspread.authorize(creds)

        return gspread_client


class GmbScraperSimple:
    def __init__(
        self,
        location,
        keyword,
        sheet_obj,
        existing_fingerprints,
        stop_event,
        status_dict
    ):
        self.location = location
        self.city = location.split(",")[-1].strip() if "," in location else location.strip()
        self.keyword = keyword
        self.keyword_sheet = sheet_obj
        self.local_scraped_fingerprints = existing_fingerprints
        self.stop_event = stop_event
        self.status_dict = status_dict
        self.task_key = f"{keyword.lower()}_{location.lower()}"

    def _update_thread_ui(self, status, last_business="-", last_action="Initializing"):
        with data_lock:
            active_workers[self.task_key] = {
                "location": self.location[:15],
                "status": status,
                "last_business": last_business[:25],
                "last_action": last_action[:22]
            }

    def _remove_thread_ui(self):
        with data_lock:
            if self.task_key in active_workers:
                del active_workers[self.task_key]

    def _is_valid_mobile(self, phone_str):
        try:
            parsed_number = phonenumbers.parse(phone_str, "IN")

            if not phonenumbers.is_valid_number(parsed_number):
                return False

            return (
                phonenumbers.number_type(parsed_number)
                == phonenumbers.PhoneNumberType.MOBILE
            )

        except phonenumbers.NumberParseException:
            return False

    def _update_global_status(self, increment_scraped=0, increment_skipped=0):
        with data_lock:
            self.status_dict["scraped"] += increment_scraped
            self.status_dict["skipped"] += increment_skipped

    def _append_to_sheet(self, row_data):
        for _ in range(RETRY_LIMIT):
            try:
                with google_sheets_lock:
                    self.keyword_sheet.append_row(
                        row_data,
                        value_input_option="USER_ENTERED"
                    )

                return True

            except Exception:
                time.sleep(2)

        return False

    def _scrape_feed(self, page):
        feed_selector = "div[role='feed']"

        try:
            if self.stop_event.is_set():
                return

            try:
                page.locator("button:has-text('Accept all')").click(timeout=1500)
            except Exception:
                pass

            page.wait_for_selector(
                "div[role='article'], a.hfpxzc",
                timeout=10000,
                state="visible"
            )

            self._update_thread_ui(
                "Scraping",
                last_action="Scrolling & extracting..."
            )

            feed_container = page.locator(feed_selector)

            if feed_container.count() > 0:
                feed_container.hover(timeout=1500)

            scraped_in_this_run = set()

            for _ in range(25):
                if self.stop_event.is_set():
                    return

                cards = page.locator("div[role='article']").all()

                if not cards:
                    cards = page.locator("a.hfpxzc").all()

                for card in cards:
                    if self.stop_event.is_set():
                        return

                    try:
                        card_text = card.inner_text(timeout=500)

                        lines = [
                            line.strip()
                            for line in card_text.split("\n")
                            if line.strip()
                        ]

                        if not lines:
                            continue

                        name = lines[0]

                        if name in scraped_in_this_run:
                            continue

                        self._update_thread_ui(
                            "Extracting",
                            last_business=name,
                            last_action="Parsing details..."
                        )

                        phone = ""
                        website = ""
                        address = ""

                        # Fast extract phone via regex from card preview text
                        phone_matches = re.findall(
                            r"(?:\+91[\-\s]?)?[6-9]\d{4}[\-\s]?\d{5}",
                            card_text
                        )
                        if phone_matches:
                            phone = phone_matches[0]

                        # Click card to open detail panel
                        panel_loaded = False
                        try:
                            card.click(timeout=2000, force=True)
                            page.wait_for_timeout(500)

                            clean_search_name = re.sub(r"[^\w\s]", "", name[:15]).strip()
                            page.wait_for_selector(
                                f"div[role='main'] h1:has-text('{clean_search_name}')",
                                timeout=3000
                            )
                            panel_loaded = True
                        except Exception:
                            panel_loaded = False

                        # Extract details from active detail pane
                        if panel_loaded:
                            detail_pane = page.locator("div[role='main']")

                            if not phone:
                                try:
                                    phone_loc = detail_pane.locator("button[data-item-id*='phone']").first
                                    if phone_loc.count() > 0:
                                        phone_val = phone_loc.get_attribute("aria-label", timeout=1000)
                                        if phone_val:
                                            phone = phone_val.replace("Phone:", "").strip()
                                except Exception:
                                    pass

                            try:
                                website_loc = detail_pane.locator("a[data-item-id='authority']").first
                                if website_loc.count() > 0:
                                    website = website_loc.get_attribute("href", timeout=1000) or ""
                            except Exception:
                                pass

                            # Primary Address Extraction
                            try:
                                address_loc = detail_pane.locator("button[data-item-id='address']").first
                                if address_loc.count() > 0:
                                    address_val = address_loc.get_attribute("aria-label", timeout=1000)
                                    if address_val:
                                        address = clean_address(address_val)
                            except Exception:
                                pass

                        # Address Fallback: If address was empty from side pane, parse from card block lines
                        if not address and len(lines) > 1:
                            for line in lines[1:]:
                                if any(kw in line.lower() for kw in ["road", "rd", "street", "st", "block", "sector", "plot", "floor", "delhi", "nagar", "marg", "1100"]):
                                    address = clean_address(line)
                                    break

                        # Validate mobile phone number
                        if not phone or not self._is_valid_mobile(phone):
                            scraped_in_this_run.add(name)
                            self._update_global_status(increment_skipped=1)

                            self._update_thread_ui(
                                "Extracting",
                                last_business=name,
                                last_action="[yellow]Skipped (Invalid)[/yellow]"
                            )
                            continue

                        fingerprint = make_fingerprint(name, phone, self.location)
                        scraped_in_this_run.add(name)

                        with data_lock:
                            if fingerprint in self.local_scraped_fingerprints:
                                is_duplicate = True
                            else:
                                self.local_scraped_fingerprints.add(fingerprint)
                                is_duplicate = False

                        if is_duplicate:
                            self._update_global_status(increment_skipped=1)

                            self._update_thread_ui(
                                "Extracting",
                                last_business=name,
                                last_action="[yellow]Skipped (Already in Sheet)[/yellow]"
                            )
                            continue

                        # Parse city from address or default to location search city
                        city_match = re.search(
                            r",\s*([^,]+),\s*[A-Za-z .()'-]+\s+\d{6}\s*$",
                            address
                        )

                        city = (
                            city_match.group(1).strip()
                            if city_match
                            else self.city
                        )

                        chain_status = is_chain_listing(name)

                        row = [
                            name,
                            address,
                            phone,
                            website,
                            self.keyword,
                            self.location,
                            city,
                            chain_status
                        ]

                        if self._append_to_sheet(row):
                            self._update_global_status(increment_scraped=1)

                            update_log(
                                f"[bold green]Saved:[/] {name[:18]} ({phone})"
                            )

                            self._update_thread_ui(
                                "Extracting",
                                last_business=name,
                                last_action="[bold green]Saved![/bold green]"
                            )
                        else:
                            with data_lock:
                                self.local_scraped_fingerprints.discard(fingerprint)

                            self._update_thread_ui(
                                "Extracting",
                                last_business=name,
                                last_action="[red]Sheet save failed[/red]"
                            )

                    except Exception:
                        continue

                page.mouse.wheel(0, 1200)
                time.sleep(1.0)

                if page.locator(
                    "text=You've reached the end of the list"
                ).count() > 0:
                    break

        except TimeoutError:
            update_log(f"[red]Timeout feed: {self.location}[/red]")

        except Exception as e:
            update_log(f"[red]Feed error: {str(e)[:100]}[/red]")

    def run(self):
        self._update_thread_ui(
            "Launching",
            last_action="Starting Chromium..."
        )

        if self.stop_event.is_set():
            self._remove_thread_ui()
            return

        query = urllib.parse.quote_plus(
            f"{self.keyword} in {self.location}"
        )

        search_url = f"https://www.google.com/maps/search/{query}"

        browser = None
        context = None

        try:
            with sync_playwright() as p:
                self._update_thread_ui(
                    "Launching",
                    last_action="Opening browser..."
                )

                browser = p.chromium.launch(
                    headless=HEADLESS_MODE,
                    timeout=30000,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox"
                    ]
                )

                self._update_thread_ui(
                    "Loading Maps",
                    last_action="Creating page..."
                )

                context = browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 720}
                )

                page = context.new_page()
                page.set_default_timeout(PLAYWRIGHT_TIMEOUT)

                try:
                    self._update_thread_ui(
                        "Loading Maps",
                        last_action="Opening Google Maps..."
                    )

                    page.goto(
                        search_url,
                        timeout=30000,
                        wait_until="domcontentloaded"
                    )

                    self._update_thread_ui(
                        "Loading Maps",
                        last_action="Waiting for results..."
                    )

                    self._scrape_feed(page)

                except Exception as e:
                    update_log(
                        f"[red]Maps error ({self.location}): "
                        f"{str(e)[:100]}[/red]"
                    )

                    self._update_thread_ui(
                        "Failed",
                        last_action="[red]Maps load failed[/red]"
                    )

        except Exception as e:
            update_log(
                f"[red]Browser error ({self.location}): "
                f"{str(e)[:100]}[/red]"
            )

            self._update_thread_ui(
                "Failed",
                last_action="[red]Browser failed[/red]"
            )

        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass

            try:
                if browser:
                    browser.close()
            except Exception:
                pass

            self._remove_thread_ui()


def prepare_keyword_sheet(keyword):
    client = get_sheets_client()
    tab_name = keyword.title()[:50]

    header_row = [
        "Name",
        "Address",
        "Phone",
        "Website",
        "Keyword",
        "Location",
        "City",
        "Is Chain"
    ]

    with google_sheets_lock:
        spreadsheet = client.open(MASTER_SHEET_NAME)

        try:
            sheet = spreadsheet.worksheet(tab_name)

        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(
                title=tab_name,
                rows=1,
                cols=8
            )

        # Force header explicit write to Row 1
        records = sheet.get_all_values()

        if not records or len(records) == 0 or records[0] != header_row:
            sheet.update("A1:H1", [header_row])
            records = sheet.get_all_values()

        existing_fingerprints = set()

        if records and len(records) > 1:
            for row in records[1:]:
                if len(row) >= 6 and row[0].strip() and row[2].strip() and row[5].strip():
                    existing_fingerprints.add(
                        make_fingerprint(row[0], row[2], row[5])
                    )

        return sheet, existing_fingerprints


def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")

    console.print(
        "[bold cyan]GMB PRO SCRAPER ENGINE[/bold cyan] "
        "[dim](Press Ctrl+C or ENTER to stop cleanly)[/dim]\n"
    )

    user_locations_input = console.input(
        "[bold yellow]Enter Location(s): [/bold yellow]"
    ).strip()

    user_keywords_input = console.input(
        "[bold yellow]Enter Keyword(s): [/bold yellow]"
    ).strip()

    if not user_locations_input or not user_keywords_input:
        sys.exit(1)

    locations = [
        location.strip()
        for location in user_locations_input.split(",")
        if location.strip()
    ]

    keywords = [
        keyword.strip()
        for keyword in user_keywords_input.split(",")
        if keyword.strip()
    ]

    completed_tasks = load_progress()
    stop_event = threading.Event()
    status = {"scraped": 0, "skipped": 0}

    def signal_handler(sig, frame):
        stop_event.set()
        update_log("[bold red]Stop signal received! Shutting down...[/bold red]")

    signal.signal(signal.SIGINT, signal_handler)

    def listen_for_keyboard():
        while not stop_event.is_set():
            try:
                line = sys.stdin.readline()

                if line:
                    stop_event.set()
                    update_log(
                        "[bold red]Enter pressed! Stopping operations...[/bold red]"
                    )
                    break

            except Exception:
                break

    input_thread = threading.Thread(
        target=listen_for_keyboard,
        daemon=True
    )

    input_thread.start()

    total_task_count = 0

    for kw in keywords:
        for loc in locations:
            if f"{kw.lower()}_{loc.lower()}" not in completed_tasks:
                total_task_count += 1

    if total_task_count == 0:
        console.print(
            "[bold green]All specified targets already completed![/bold green]"
        )
        sys.exit(0)

    completed_counter = 0
    current_keyword_display = "-"

    def render_compact_dashboard():
        table = Table(
            box=None,
            show_header=True,
            header_style="bold blue",
            expand=True,
            pad_edge=False
        )

        table.add_column("Location", style="cyan", ratio=2)
        table.add_column("State", justify="center", style="bold blue", ratio=1)
        table.add_column("Business Checked", style="yellow", ratio=3)
        table.add_column("Action / Status", style="white", ratio=3)

        with data_lock:
            if not active_workers:
                table.add_row("[dim]Starting batch...[/dim]", "-", "-", "-")

            else:
                for _, worker in list(active_workers.items()):
                    table.add_row(
                        worker["location"],
                        worker["status"],
                        worker["last_business"],
                        worker["last_action"]
                    )

        pct = (
            completed_counter / total_task_count
            if total_task_count > 0
            else 0
        )

        filled = int(pct * 20)
        bar = f"[{'█' * filled}{'░' * (20 - filled)}]"

        header_str = (
            f"[bold cyan]KEYWORD:[/] "
            f"[bold yellow]{current_keyword_display.upper()}[/]  |  "
            f"[bold cyan]PROGRESS:[/] [green]{bar}[/] "
            f"{int(pct * 100)}% ({completed_counter}/{total_task_count})\n"
            f"[bold cyan]STATS:[/] "
            f"[bold green]Saved: {status['scraped']}[/]  •  "
            f"[bold red]Skipped: {status['skipped']}[/]  •  "
            f"[bold yellow]Remaining: "
            f"{total_task_count - completed_counter}[/]"
        )

        content_grid = Table.grid(expand=True)
        content_grid.add_row(header_str)
        content_grid.add_row("─" * 70)
        content_grid.add_row(table)
        content_grid.add_row("─" * 70)
        content_grid.add_row(f"[dim]{latest_log}[/dim]")

        return Panel(
            content_grid,
            title="[bold white]GMB SCRAPER LIVE DASHBOARD[/bold white]",
            border_style="cyan",
            padding=(0, 1)
        )

    with Live(
        render_compact_dashboard(),
        refresh_per_second=4,
        console=console
    ) as live:
        for keyword in keywords:
            if stop_event.is_set():
                break

            current_keyword_display = keyword

            pending_locations = [
                loc
                for loc in locations
                if f"{keyword.lower()}_{loc.lower()}" not in completed_tasks
            ]

            if not pending_locations:
                continue

            update_log(f"Opening sheet tab for: '{keyword.title()}'")
            live.update(render_compact_dashboard())

            try:
                sheet_obj, existing_fingerprints = prepare_keyword_sheet(keyword)

            except Exception as e:
                update_log(f"[red]Sheet Error for {keyword}: {e}[/red]")
                continue

            for batch_locations in chunk_list(pending_locations, MAX_THREADS):
                if stop_event.is_set():
                    break

                with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                    futures_map = {}

                    for loc in batch_locations:
                        task_id = f"{keyword.lower()}_{loc.lower()}"

                        scraper = GmbScraperSimple(
                            location=loc,
                            keyword=keyword,
                            sheet_obj=sheet_obj,
                            existing_fingerprints=existing_fingerprints,
                            stop_event=stop_event,
                            status_dict=status
                        )

                        future = executor.submit(scraper.run)
                        futures_map[future] = task_id

                    while futures_map and not stop_event.is_set():
                        done_futures = [
                            future
                            for future in futures_map
                            if future.done()
                        ]

                        for future in done_futures:
                            task_id = futures_map.pop(future)

                            try:
                                future.result()

                                if not stop_event.is_set():
                                    save_completed_task(task_id)
                                    completed_counter += 1

                            except Exception as e:
                                update_log(f"[red]Task Error: {e}[/red]")

                        live.update(render_compact_dashboard())
                        time.sleep(0.2)

                live.update(render_compact_dashboard())

    console.print(
        "\n[bold green]✔ All tasks completed or safely stopped![/bold green]"
    )