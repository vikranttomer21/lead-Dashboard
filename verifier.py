import os
import sys
import re
import time
import json
import urllib.parse
import asyncio
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import phonenumbers
from playwright.async_api import async_playwright
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

# ---- CONFIG ----
HEADLESS_MODE = True  
SERVICE_ACCOUNT_FILE = "service_account.json"
MASTER_SHEET_NAME = "India Data"
MAX_CONCURRENT_TABS = 5
BATCH_UPDATE_SIZE = 10
MAX_VERIFICATION_PASSES = 2
PLAYWRIGHT_TIMEOUT = 10000
DOM_ACTION_TIMEOUT = 3000
PROGRESS_FILE = "verifier_progress.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]

console = Console()
gspread_client = None
latest_log = "Initializing Engine..."
stop_flag = False

stats = {
    "verified": 0,
    "corrected": 0,
    "duplicates_deleted": 0,
    "last_flushed": 0
}

active_workers = {i: {"business": "-", "status": "[dim]Idle[/dim]"} for i in range(1, MAX_CONCURRENT_TABS + 1)}

OUTPUT_HEADERS = [
    "Name", "Address", "Phone", "Website", "Keyword",
    "Location", "City", "Is Chain", "Entity Status"
]


def update_log(msg):
    global latest_log
    timestamp = datetime.now().strftime("%H:%M:%S")
    latest_log = f"[{timestamp}] {msg}"


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_progress_item(tab_name, lead_key, verified_data):
    data = load_progress()
    if tab_name not in data:
        data[tab_name] = {}
    data[tab_name][lead_key] = verified_data
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_sheets_client():
    global gspread_client
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


def clean_address(address_str):
    if not address_str:
        return ""
    cleaned = re.sub(r"^Address:\s*", "", str(address_str), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    return cleaned.strip()


def normalize_phone(phone_str):
    if not phone_str:
        return ""
    try:
        parsed = phonenumbers.parse(str(phone_str), "IN")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except Exception:
        pass
    clean_digits = re.sub(r"\D", "", str(phone_str))
    return clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits


def extract_root_domain(url):
    if not url:
        return ""
    url = str(url).strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.split("/")[0].split("?")[0]


def clean_brand_name(name):
    name_clean = name.lower()
    name_clean = re.sub(r"[^\w\s]", " ", name_clean)
    words = [
        w for w in name_clean.split()
        if w not in ["the", "by", "bar", "cafe", "restaurant", "lounge", "clinic", "dental", "and", "&"]
    ]
    return " ".join(words[:2]) if words else name_clean[:10]


def calculate_lead_score(lead):
    score = 0
    name = lead[0] if len(lead) > 0 else ""
    address = lead[1] if len(lead) > 1 else ""
    phone = lead[2] if len(lead) > 2 else ""
    website = lead[3] if len(lead) > 3 else ""

    if phone and len(phone) >= 10:
        score += 30
    if website and "grexa.site" not in website and "business.site" not in website:
        score += 35
    elif website:
        score += 15
    if len(address) > 25:
        score += 25
    if name and not any(p in name.lower() for p in ["best", "near me", "cghs"]):
        score += 10
    return score


def resolve_entities_and_deduplicate(verified_rows):
    phone_groups = {}
    web_groups = {}

    for row in verified_rows:
        if not row:
            continue
        p_norm = normalize_phone(row[2])
        w_root = extract_root_domain(row[3])
        if p_norm:
            phone_groups.setdefault(p_norm, []).append(row)
        if w_root:
            web_groups.setdefault(w_root, []).append(row)

    discard_signatures = set()

    # 1. Resolve Shared Phone Groups
    for phone_key, group in phone_groups.items():
        if len(group) > 1:
            brand_map = {}
            for row in group:
                brand_key = clean_brand_name(row[0])
                loc_key = row[5].strip().lower()
                brand_map.setdefault((brand_key, loc_key), []).append(row)

            for (brand_key, loc_key), duplicates in brand_map.items():
                if len(duplicates) > 1:
                    ranked = sorted(duplicates, key=calculate_lead_score, reverse=True)
                    for loser in ranked[1:]:
                        loser_sig = f"{loser[0]}|{loser[2]}|{loser[5]}"
                        if loser_sig not in discard_signatures:
                            discard_signatures.add(loser_sig)
                            stats["duplicates_deleted"] += 1
                            update_log(f"[bold red]Deleted Exact Dup:[/] {loser[0][:20]}")

            for row in group:
                sig = f"{row[0]}|{row[2]}|{row[5]}"
                if sig not in discard_signatures:
                    row[8] = f"Shared Contact ({len(group)} Outlets)"

    # 2. Resolve Shared Website Groups
    for root_domain, group in web_groups.items():
        if len(group) > 1:
            for row in group:
                sig = f"{row[0]}|{row[2]}|{row[5]}"
                if sig not in discard_signatures:
                    if "Shared Contact" in row[8]:
                        row[8] += " / Shared Domain"
                    else:
                        row[8] = f"Parent Domain ({root_domain[:15]})"

    final_cleaned = []
    for row in verified_rows:
        if not row:
            continue
        sig = f"{row[0]}|{row[2]}|{row[5]}"
        if sig not in discard_signatures:
            final_cleaned.append(row)

    return final_cleaned


def stream_batch_to_sheet(sheet, verified_rows, tab_name):
    """Dynamically writes ongoing verified rows to Google Sheets."""
    if not verified_rows:
        return
    full_table = [OUTPUT_HEADERS] + verified_rows
    try:
        sheet.update(range_name=f"A1:I{len(full_table)}", values=full_table)
        stats["last_flushed"] = len(verified_rows)
        update_log(f"[bold green]Dynamically updated {len(verified_rows)} leads on '{tab_name}'[/bold green]")
    except Exception as e:
        update_log(f"[red]Batch update warning: {str(e)[:40]}[/red]")


async def verify_lead_async(worker_id, context, row, tab_name, semaphore):
    global stop_flag
    if stop_flag:
        return None

    name = row[0].strip() if len(row) > 0 else ""
    address = row[1].strip() if len(row) > 1 else ""
    phone = row[2].strip() if len(row) > 2 else ""
    website = row[3].strip() if len(row) > 3 else ""
    keyword = row[4].strip() if len(row) > 4 else tab_name
    location = row[5].strip() if len(row) > 5 else ""
    city = row[6].strip() if len(row) > 6 else ""
    is_chain = row[7].strip() if len(row) > 7 else "No"
    entity_status = "Direct Business"

    lead_key = f"{name.lower()}|{location.lower()}"

    # Cache hit check
    saved_progress = load_progress().get(tab_name, {})
    if lead_key in saved_progress:
        stats["verified"] += 1
        active_workers[worker_id] = {
            "business": name[:20],
            "status": "[dim green]Loaded Cache[/dim green]"
        }
        return saved_progress[lead_key]

    async with semaphore:
        if stop_flag:
            return None

        active_workers[worker_id] = {
            "business": name[:20],
            "status": "[cyan]Searching Maps...[/cyan]"
        }

        search_query = f"{name} {address if address else location}"
        encoded_query = urllib.parse.quote_plus(search_query)
        search_url = f"https://www.google.com/maps/search/{encoded_query}"

        verified_address = address
        verified_phone = phone
        verified_website = website
        corrected = False

        page = await context.new_page()
        page.set_default_timeout(DOM_ACTION_TIMEOUT)

        try:
            for _ in range(MAX_VERIFICATION_PASSES):
                if stop_flag:
                    break
                try:
                    await page.goto(search_url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="domcontentloaded")

                    try:
                        accept_btn = page.locator("button:has-text('Accept all')")
                        if await accept_btn.count() > 0:
                            await accept_btn.click(timeout=800)
                    except Exception:
                        pass

                    cards = page.locator("div[role='article'], a.hfpxzc")
                    if await cards.count() > 0:
                        await cards.first.click(timeout=1200, force=True)
                        await page.wait_for_timeout(350)

                    clean_name = re.sub(r"[^\w\s]", "", name[:12]).strip()
                    if clean_name:
                        try:
                            await page.wait_for_selector(f"div[role='main'] h1:has-text('{clean_name}')", timeout=DOM_ACTION_TIMEOUT)
                        except Exception:
                            pass

                    detail_pane = page.locator("div[role='main']")

                    # Phone extraction
                    try:
                        p_loc = detail_pane.locator("button[data-item-id*='phone']").first
                        if await p_loc.count() > 0:
                            raw_p = await p_loc.get_attribute("aria-label", timeout=600)
                            if raw_p:
                                p_val = raw_p.replace("Phone:", "").strip()
                                if p_val and p_val != phone:
                                    verified_phone = p_val
                                    corrected = True
                    except Exception:
                        pass

                    # Website extraction
                    try:
                        w_loc = detail_pane.locator("a[data-item-id='authority']").first
                        if await w_loc.count() > 0:
                            w_val = await w_loc.get_attribute("href", timeout=600) or ""
                            if w_val and w_val != website:
                                verified_website = w_val
                                corrected = True
                    except Exception:
                        pass

                    # Address extraction
                    try:
                        a_loc = detail_pane.locator("button[data-item-id='address']").first
                        if await a_loc.count() > 0:
                            raw_a = await a_loc.get_attribute("aria-label", timeout=600)
                            if raw_a:
                                a_val = clean_address(raw_a)
                                if a_val and a_val != address:
                                    verified_address = a_val
                                    corrected = True
                    except Exception:
                        pass

                    break

                except Exception:
                    await asyncio.sleep(0.3)

        except Exception:
            pass
        finally:
            await page.close()

        if verified_address:
            city_match = re.search(r",\s*([^,]+),\s*[A-Za-z .()'-]+\s+\d{6}\s*$", verified_address)
            if city_match:
                city = city_match.group(1).strip()

        final_row = [
            name,
            verified_address,
            verified_phone,
            verified_website,
            keyword,
            location,
            city,
            is_chain,
            entity_status
        ]

        save_progress_item(tab_name, lead_key, final_row)

        stats["verified"] += 1
        if corrected:
            stats["corrected"] += 1
            update_log(f"[bold cyan]Corrected:[/] {name[:18]}")

        active_workers[worker_id] = {
            "business": name[:20],
            "status": "[green]Verified[/green]"
        }

        return final_row


def render_dashboard(current_keyword, current_idx, total_leads):
    pct = (current_idx / total_leads) if total_leads > 0 else 0
    filled = int(pct * 20)
    bar = f"[{'█' * filled}{'░' * (20 - filled)}]"

    header_str = (
        f"[bold cyan]ACTIVE KEYWORD:[/] [bold yellow]{current_keyword.upper()}[/]  |  "
        f"[bold cyan]PROGRESS:[/] [green]{bar}[/] {int(pct * 100)}% ({current_idx}/{total_leads})\n"
        f"[bold cyan]STATS:[/] "
        f"[bold green]Verified: {stats['verified']}[/]  •  "
        f"[bold yellow]Fields Corrected: {stats['corrected']}[/]  •  "
        f"[bold red]Duplicates Purged: {stats['duplicates_deleted']}[/]  •  "
        f"[bold magenta]Sheet Synced: {stats['last_flushed']}[/]"
    )

    table = Table(
        box=None,
        show_header=True,
        header_style="bold blue",
        expand=True,
        pad_edge=False
    )
    table.add_column("Tab Slot", style="cyan", ratio=2)
    table.add_column("Current Business", style="yellow", ratio=3)
    table.add_column("Live Status", style="white", ratio=3)

    for w_id in range(1, MAX_CONCURRENT_TABS + 1):
        w_info = active_workers.get(w_id, {"business": "-", "status": "[dim]Idle[/dim]"})
        table.add_row(f"Worker #{w_id}", w_info["business"], w_info["status"])

    content_grid = Table.grid(expand=True)
    content_grid.add_row(header_str)
    content_grid.add_row("─" * 70)
    content_grid.add_row(table)
    content_grid.add_row("─" * 70)
    content_grid.add_row(f"[dim]{latest_log}[/dim]")

    return Panel(
        content_grid,
        title="[bold white]GMB ASYNC 5-TAB VERIFIER & DEDUPLICATOR[/bold white]",
        border_style="cyan",
        padding=(0, 1)
    )


async def listen_for_enter():
    global stop_flag
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, sys.stdin.readline)
    stop_flag = True
    update_log("[bold red]Enter key detected! Halting operations...[/bold red]")


async def process_keyword_sheet_async(sheet, keyword_name, live):
    global stop_flag
    if stop_flag:
        return

    all_values = sheet.get_all_values()
    if not all_values or len(all_values) <= 1:
        update_log(f"No records found in tab: {keyword_name}")
        return

    raw_rows = all_values[1:]
    total_leads = len(raw_rows)

    update_log(f"Starting 5-tab dynamic worker on '{keyword_name}' ({total_leads} leads)...")
    live.update(render_dashboard(keyword_name, 0, total_leads))

    # Initialize Header on Sheet
    if not all_values or all_values[0] != OUTPUT_HEADERS:
        sheet.update(range_name="A1:I1", values=[OUTPUT_HEADERS])

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TABS)
    verified_rows = []
    unflushed_count = 0
    keyword_verified_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS_MODE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 720})

        tasks = []
        for idx, row in enumerate(raw_rows):
            worker_id = (idx % MAX_CONCURRENT_TABS) + 1
            tasks.append(
                asyncio.create_task(
                    verify_lead_async(worker_id, context, row, keyword_name, semaphore)
                )
            )

        for finished_task in asyncio.as_completed(tasks):
            if stop_flag:
                break
            res = await finished_task
            if res:
                verified_rows.append(res)
                unflushed_count += 1
                keyword_verified_count += 1

            # Dynamic batch update every 10 verified rows
            if unflushed_count >= BATCH_UPDATE_SIZE:
                stream_batch_to_sheet(sheet, verified_rows, keyword_name)
                unflushed_count = 0

            live.update(render_dashboard(keyword_name, keyword_verified_count, total_leads))

        await context.close()
        await browser.close()

    if stop_flag:
        update_log(f"[bold yellow]Paused tab '{keyword_name}'. Progress saved in verifier_progress.json.[/bold yellow]")
        return

    # ---- POST-KEYWORD DEDUPLICATION & FINAL CLEANUP ----
    update_log(f"[bold yellow]Running Deduplication & Entity Resolution on '{keyword_name}'...[/bold yellow]")
    live.update(render_dashboard(keyword_name, total_leads, total_leads))

    final_clean_rows = resolve_entities_and_deduplicate(verified_rows)

    update_log(f"Writing {len(final_clean_rows)} final clean rows to '{keyword_name}'...")
    sheet.clear()
    full_table = [OUTPUT_HEADERS] + final_clean_rows
    sheet.update(range_name=f"A1:I{len(full_table)}", values=full_table)

    update_log(f"[bold green]✔ Tab '{keyword_name}' finalized and deduplicated! Moving to next tab...[/bold green]")
    live.update(render_dashboard(keyword_name, total_leads, total_leads))
    await asyncio.sleep(1.0)


async def main_async(target_worksheets):
    asyncio.create_task(listen_for_enter())

    with Live(render_dashboard("-", 0, 0), refresh_per_second=4, console=console) as live:
        for ws in target_worksheets:
            if stop_flag:
                break
            await process_keyword_sheet_async(ws, ws.title, live)

    if stop_flag:
        console.print("\n[bold yellow]✔ Engine stopped cleanly. All verified progress is safely cached.[/bold yellow]")
    else:
        console.print("\n[bold green]✔ All selected sheets verified, batch-updated, deduplicated, and finalized![/bold green]")


if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")

    console.print("[bold cyan]══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold white]   GMB ASYNC 5-TAB DYNAMIC BATCH VERIFIER & CLEANER      [/bold white]")
    console.print("[bold cyan]══════════════════════════════════════════════════════════[/bold cyan]\n")

    client = get_sheets_client()
    spreadsheet = client.open(MASTER_SHEET_NAME)
    all_worksheets = spreadsheet.worksheets()

    if not all_worksheets:
        console.print("[bold red]No worksheets found in Google Sheet![/bold red]")
        sys.exit(1)

    console.print("[bold yellow]Available Keyword Tabs in Google Sheets:[/bold yellow]")
    for idx, ws in enumerate(all_worksheets, start=1):
        console.print(f" [cyan]{idx}.[/cyan] {ws.title}")

    console.print("\n[dim]Enter numbers (e.g. 1, 3), exact names, or 'ALL':[/dim]")
    user_choice = console.input("[bold yellow]Select Keywords to verify: [/bold yellow]").strip()

    target_worksheets = []

    if user_choice.upper() == "ALL" or not user_choice:
        target_worksheets = all_worksheets
    else:
        chosen_tokens = [token.strip() for token in user_choice.split(",") if token.strip()]
        for token in chosen_tokens:
            if token.isdigit():
                sheet_idx = int(token) - 1
                if 0 <= sheet_idx < len(all_worksheets):
                    target_worksheets.append(all_worksheets[sheet_idx])
            else:
                for ws in all_worksheets:
                    if ws.title.lower() == token.lower():
                        target_worksheets.append(ws)

    if not target_worksheets:
        console.print("[bold red]No matching keyword tabs selected. Exiting.[/bold red]")
        sys.exit(0)

    asyncio.run(main_async(target_worksheets))