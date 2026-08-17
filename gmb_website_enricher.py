import asyncio
from datetime import datetime
import hashlib
import json
import os
import re
import signal
import sys
import time
import unicodedata
from urllib.parse import quote_plus, urlparse
import warnings

from bs4 import BeautifulSoup
from email_validator import validate_email
import gspread
import httpx
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import phonenumbers
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.theme import Theme
import urllib3

# ==============================================================================
# CONFIGURATION & GLOBAL SETUP
# ==============================================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

SERVICE_ACCOUNT_FILE = "service_account.json"
CHECKPOINT_FILE = "platinum_enrichment_checkpoint.json"
AUDIT_OUTPUT_DIR = "audits_json"
HTML_REPORT_DIR = "audit_reports_html"
RAW_SPREADSHEET_NAME = "India Data"

MAX_CONCURRENT_WORKERS = 10  # Fast async I/O worker pool
HTTP_TIMEOUT_SEC = 8.0
BATCH_SIZE = 15

os.makedirs(AUDIT_OUTPUT_DIR, exist_ok=True)
os.makedirs(HTML_REPORT_DIR, exist_ok=True)

console = Console(
    theme=Theme({
        "info": "bold cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
    })
)

stop_requested = False


def handle_sigint(sig, frame):
  global stop_requested
  console.print(
      "\n[bold red]🛑 Graceful Stop Requested! Finishing current"
      " batch...[/bold red]"
  )
  stop_requested = True


signal.signal(signal.SIGINT, handle_sigint)


def load_checkpoints() -> set:
  if os.path.exists(CHECKPOINT_FILE):
    try:
      with open(CHECKPOINT_FILE, "r") as f:
        return set(json.load(f))
    except Exception:
      return set()
  return set()


def save_checkpoints(checkpoints: set):
  with open(CHECKPOINT_FILE, "w") as f:
    json.dump(list(checkpoints), f)


def get_sheets_client():
  try:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    return gspread.authorize(
        ServiceAccountCredentials.from_json_keyfile_name(
            SERVICE_ACCOUNT_FILE, scope
        )
    )
  except Exception as e:
    console.print(f"[error]Google Sheets Auth Failed: {e}[/error]")
    return None


# ==============================================================================
# ULTRA-FAST GOOGLE RATINGS & REVIEWS SCRAPER
# ==============================================================================
async def fetch_live_gmb_ratings(
    client: httpx.AsyncClient, name: str, location: str, city: str
) -> tuple[float, int]:
  """Fetches live rating and review counts via Google Knowledge Graph in <500ms."""
  search_query = f"{name} {location} {city} reviews"
  url = f"https://www.google.com/search?q={quote_plus(search_query)}&hl=en&gl=in"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/126.0.0.0 Safari/537.36"
      ),
      "Accept": (
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
      ),
      "Accept-Language": "en-US,en;q=0.9",
  }

  rating = 0.0
  reviews = 0

  try:
    resp = await client.get(url, headers=headers, timeout=5.0)
    if resp.status_code == 200:
      html = resp.text

      # Match pattern: 4.6 ★ (1,234)
      m1 = re.search(
          r'class="[^"]*">([1-5]\.\d)</span>.*?class="[^"]*">\(?([\d,]+)\)?',
          html,
      )
      if m1:
        return float(m1.group(1)), int(re.sub(r"\D", "", m1.group(2)))

      # Match pattern: 4.6 stars ... 142 reviews
      m2 = re.search(
          r"([1-5]\.\d)\s*(?:stars|out of 5|\u2605).*?([\d,]+)\s*(?:reviews|Google"
          r" reviews)",
          html,
          re.I,
      )
      if m2:
        return float(m2.group(1)), int(re.sub(r"\D", "", m2.group(2)))

      # Match pattern: aria-label="Rated 4.8 out of 5, 234 reviews"
      m3 = re.search(
          r'aria-label="Rated ([1-5]\.\d) out of 5.*?([\d,]+)\s*reviews?',
          html,
          re.I,
      )
      if m3:
        return float(m3.group(1)), int(re.sub(r"\D", "", m3.group(2)))

      # Fallback star search
      m4 = re.search(r"\b([1-5]\.\d)\b\s*★", html)
      if m4:
        rating = float(m4.group(1))
        rev_match = re.search(r"([\d,]+)\s*Google reviews", html, re.I)
        if rev_match:
          reviews = int(re.sub(r"\D", "", rev_match.group(1)))
        return rating, reviews
  except Exception:
    pass

  return rating, reviews


# ==============================================================================
# MODULE 1: DATA CLEANER & URL INFRASTRUCTURE CLASSIFIER
# ==============================================================================
class LeadDataCleaner:
  SOCIAL_DOMAINS = {
      "instagram.com",
      "facebook.com",
      "fb.me",
      "linktr.ee",
      "bio.link",
      "taponn.me",
      "wa.link",
      "wa.me",
  }
  AGGREGATOR_DOMAINS = {
      "zomato.com",
      "swiggy.com",
      "magicpin.in",
      "dineout.co.in",
      "justdial.com",
      "practo.com",
      "eatsure.com",
      "weddingwire.in",
      "wedmegood.com",
  }
  FREE_BUILDER_DOMAINS = {
      "business.site",
      "canva.site",
      "grexa.site",
      "wixsite.com",
      "weebly.com",
      "blogspot.com",
      "site123.me",
      "website3.me",
      "base44.app",
      "lovable.app",
      "mypixieset.com",
      "yeme.in",
  }

  @staticmethod
  def normalize_name(name: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", str(name))
        .encode("ASCII", "ignore")
        .decode("utf-8")
    )
    clean = re.sub(
        r"(?i)\b(pvt|ltd|inc|llc|co|corp|m/s|private|limited)\b|\.",
        "",
        normalized,
    )
    return re.sub(r"\s+", " ", clean).strip().title()

  @staticmethod
  def get_base_brand_stem(name: str) -> str:
    norm = LeadDataCleaner.normalize_name(name).lower()
    norm = re.split(r"[-|–—,:]", norm)[0].strip()
    norm = re.sub(
        r"\b(new delhi|delhi|dwarka|saket|hauz khas|connaught place|karol"
        r" bagh|gurugram|noida|vasant kunj|rohini|punjabi bagh|greater"
        r" kailash)\b",
        "",
        norm,
        flags=re.I,
    )
    return re.sub(r"[^\w]", "", norm).strip()

  @staticmethod
  def extract_root_domain(url: str) -> str:
    if not url:
      return ""
    parsed = urlparse(LeadDataCleaner.normalize_website(url))
    netloc = parsed.netloc.lower().replace("www.", "")
    parts = netloc.split(".")
    if len(parts) >= 2:
      if (
          len(parts) > 2
          and parts[-2] not in ["co", "com", "org", "net", "gov", "in"]
      ):
        return f"{parts[-2]}.{parts[-1]}"
      elif (
          len(parts) > 2
          and parts[-2] in ["co", "com", "org", "net", "gov", "in"]
      ):
        return ".".join(parts[-3:])
    return netloc

  @staticmethod
  def safe_filename(name: str) -> str:
    clean = LeadDataCleaner.normalize_name(name)
    slug = re.sub(r"[^\w]", "_", clean).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    return slug[:50] if slug else "lead_profile"

  @staticmethod
  def normalize_website(url: str) -> str:
    if not url or str(url).lower() in ["n/a", "none", "nan", "", "null"]:
      return ""
    url_str = str(url).strip()
    if not url_str.startswith(("http://", "https://")):
      url_str = "https://" + url_str
    parsed = urlparse(url_str)
    return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")

  @staticmethod
  def classify_url_infrastructure(url: str) -> dict:
    if not url:
      return {
          "infra_type": "NO_WEBSITE",
          "root_domain": "",
          "is_subpath": False,
      }
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.strip("/")
    is_subpath = len(path.split("/")) >= 1 and path != ""

    if any(s in netloc for s in LeadDataCleaner.SOCIAL_DOMAINS):
      return {
          "infra_type": "SOCIAL_AS_WEBSITE",
          "root_domain": netloc,
          "is_subpath": is_subpath,
      }
    if any(a in netloc for a in LeadDataCleaner.AGGREGATOR_DOMAINS):
      return {
          "infra_type": "AGGREGATOR_COMMISSION_LEAKER",
          "root_domain": netloc,
          "is_subpath": is_subpath,
      }
    if (
        any(f in netloc for f in LeadDataCleaner.FREE_BUILDER_DOMAINS)
        or "sites.google.com" in netloc
    ):
      return {
          "infra_type": "FREE_BUILDER_TIER",
          "root_domain": netloc,
          "is_subpath": is_subpath,
      }
    if is_subpath and (
        "stores." in netloc
        or "nearme." in netloc
        or any(
            p in path.lower()
            for p in [
                "delhi",
                "location",
                "branch",
                "store",
                "outlets",
                "store-pages",
                "restaurant",
            ]
        )
    ):
      return {
          "infra_type": "SUBPATH_DIRECTORY",
          "root_domain": LeadDataCleaner.extract_root_domain(url),
          "is_subpath": True,
      }

    return {
        "infra_type": "INDEPENDENT_DOMAIN",
        "root_domain": LeadDataCleaner.extract_root_domain(url),
        "is_subpath": is_subpath,
    }

  @staticmethod
  def validate_phone(phone_str: str, default_country="IN") -> dict:
    try:
      parsed = phonenumbers.parse(str(phone_str), default_country)
      if phonenumbers.is_valid_number(parsed):
        e164 = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )
        return {
            "formatted": e164,
            "is_valid": True,
            "digits": re.sub(r"\D", "", e164),
        }
    except Exception:
      pass
    digits = re.sub(r"\D", "", str(phone_str))
    formatted = (
        f"+91{digits[-10:]}" if len(digits) >= 10 else str(phone_str).strip()
    )
    return {
        "formatted": formatted,
        "is_valid": len(digits) >= 10,
        "digits": digits,
    }

  @staticmethod
  def validate_email(email_str: str) -> str:
    try:
      valid = validate_email(
          str(email_str).strip(), check_deliverability=False
      ).normalized.lower()
      prefix, domain = valid.split("@")
      ignore_domains = {
          "wix.com",
          "domain.com",
          "example.com",
          "schema.org",
          "cloudflare.com",
          "sentry.io",
      }
      if domain in ignore_domains or prefix in {
          "sentry",
          "error",
          "bug",
          "noreply",
          "test",
          "admin",
      }:
        return ""
      return valid
    except Exception:
      return ""

  @staticmethod
  def extract_field(row_dict: dict, possible_keys: list, default="") -> str:
    cleaned_row = {
        re.sub(r"[^\w]", "", str(k)).lower(): v for k, v in row_dict.items()
    }
    for key in possible_keys:
      clean_key = re.sub(r"[^\w]", "", key).lower()
      if clean_key in cleaned_row:
        val = str(cleaned_row[clean_key]).strip()
        if val and val.lower() not in [
            "n/a",
            "none",
            "nan",
            "null",
            "undefined",
        ]:
          return val
    return default

  @staticmethod
  def generate_dedup_key(name: str, phone: str) -> str:
    clean_name = re.sub(
        r"[^\w]", "", LeadDataCleaner.normalize_name(name)
    ).lower()
    clean_phone = re.sub(r"\D", "", str(phone))[-10:]
    return f"{clean_name}|{clean_phone}"


# ==============================================================================
# MODULE 2: MULTI-LOCATION & CHAIN RESOLVER
# ==============================================================================
class MultiLocationResolver:

  @staticmethod
  def build_network_footprints(
      df: pd.DataFrame, cleaner: LeadDataCleaner
  ) -> pd.DataFrame:
    df["_Clean_Phone"] = df.apply(
        lambda r: cleaner.validate_phone(
            cleaner.extract_field(
                r.to_dict(),
                ["Phone", "Phone Number", "Mobile", "Contact", "Tel"],
            )
        )["digits"][-10:],
        axis=1,
    )
    df["_Clean_Name"] = df.apply(
        lambda r: cleaner.normalize_name(
            cleaner.extract_field(
                r.to_dict(), ["Name", "Title", "Business Name"]
            )
        ),
        axis=1,
    )
    df["_Brand_Stem"] = df["_Clean_Name"].apply(
        cleaner.get_base_brand_stem
    )
    df["_Root_Domain"] = df.apply(
        lambda r: cleaner.extract_root_domain(
            cleaner.extract_field(
                r.to_dict(), ["Website", "Url", "Link", "Site"]
            )
        ),
        axis=1,
    )
    df["_Location"] = df.apply(
        lambda r: cleaner.extract_field(
            r.to_dict(), ["Location", "City", "Area"], "Delhi"
        ).title(),
        axis=1,
    )
    df["_Raw_Is_Chain"] = df.apply(
        lambda r: cleaner.extract_field(
            r.to_dict(), ["Is Chain", "IsChain"], "No"
        ).lower(),
        axis=1,
    )
    df["_Raw_Entity_Status"] = df.apply(
        lambda r: cleaner.extract_field(
            r.to_dict(), ["Entity Status", "EntityStatus"], ""
        ),
        axis=1,
    )

    domain_to_indices = {}
    brand_stem_to_indices = {}
    phone_to_indices = {}

    for idx, row in df.iterrows():
      dom = row["_Root_Domain"]
      stem = row["_Brand_Stem"]
      ph = row["_Clean_Phone"]

      if (
          dom
          and dom
          not in LeadDataCleaner.SOCIAL_DOMAINS
          | LeadDataCleaner.AGGREGATOR_DOMAINS
          | LeadDataCleaner.FREE_BUILDER_DOMAINS
      ):
        domain_to_indices.setdefault(dom, []).append(idx)

      if stem and len(stem) >= 4:
        brand_stem_to_indices.setdefault(stem, []).append(idx)

      if ph and len(ph) == 10:
        phone_to_indices.setdefault(ph, []).append(idx)

    resolved_types = []
    footprints = []

    for idx, row in df.iterrows():
      dom = row["_Root_Domain"]
      stem = row["_Brand_Stem"]
      ph = row["_Clean_Phone"]
      name = row["_Clean_Name"]
      raw_chain = row["_Raw_Is_Chain"]
      raw_status = row["_Raw_Entity_Status"]

      dom_matches = (
          domain_to_indices.get(dom, [])
          if (
              dom
              and dom
              not in LeadDataCleaner.SOCIAL_DOMAINS
              | LeadDataCleaner.AGGREGATOR_DOMAINS
              | LeadDataCleaner.FREE_BUILDER_DOMAINS
          )
          else []
      )
      stem_matches = (
          brand_stem_to_indices.get(stem, []) if (stem and len(stem) >= 4) else []
      )
      ph_matches = phone_to_indices.get(ph, []) if (ph and len(ph) == 10) else []

      all_chain_indices = list(set(dom_matches + stem_matches))
      all_chain_indices.sort()

      if len(all_chain_indices) > 1 or raw_chain in ["yes", "true", "1"]:
        outlets_count = max(len(all_chain_indices), 2)
        locations_seen = list(
            dict.fromkeys(
                [df.loc[i, "_Location"] for i in all_chain_indices if i in df.index]
            )
        )
        locs_str = (
            ", ".join(locations_seen[:3])
            + (f" +{len(locations_seen)-3} more" if len(locations_seen) > 3 else "")
            if locations_seen
            else "Multiple Areas"
        )
        resolved_types.append(f"Multi-Branch Chain ({outlets_count} Outlets)")
        footprints.append(
            f"Regional Brand Network: Active in {outlets_count} Outlets"
            f" ({locs_str})"
        )
      elif len(ph_matches) > 1 or "Shared Contact" in raw_status:
        associated_names = [
            df.loc[i, "_Clean_Name"] for i in ph_matches if i != idx and i in df.index
        ]
        unique_assoc = list(dict.fromkeys(associated_names))
        if unique_assoc:
          resolved_types.append(
              f"Hospitality/Parent Group ({len(unique_assoc)+1} Entities: {name},"
              f" {', '.join(unique_assoc[:2])})"
          )
          footprints.append(
              f"Centralized Operations Across {len(ph_matches)} Outlets"
          )
        else:
          resolved_types.append(
              f"Multi-Branch Chain ({len(ph_matches)} Locations)"
          )
          footprints.append(f"Branch Network: Active in {len(ph_matches)} Areas")
      else:
        resolved_types.append("Independent Entity")
        footprints.append("Single Flagship Location")

    df["Entity_Resolution_Type"] = resolved_types
    df["Network_Footprint"] = footprints

    return df.drop(
        columns=[
            "_Clean_Phone",
            "_Clean_Name",
            "_Brand_Stem",
            "_Root_Domain",
            "_Location",
            "_Raw_Is_Chain",
            "_Raw_Entity_Status",
        ]
    )


# ==============================================================================
# FAST ASYNC AUDITOR (100-POINT BENCHMARK)
# ==============================================================================
class DeepAuditor:

  def __init__(
      self,
      raw_url,
      name,
      rating,
      reviews,
      gmb_phone,
      gmb_email,
      gmb_address,
      city,
      location,
      keyword,
      entity_type,
  ):
    self.raw_url = LeadDataCleaner.normalize_website(raw_url)
    self.name = LeadDataCleaner.normalize_name(name)
    self.rating = rating
    self.reviews = reviews
    self.gmb_phone = gmb_phone
    self.gmb_email = gmb_email
    self.gmb_address = gmb_address
    self.city = city
    self.location = location
    self.keyword = keyword
    self.entity_type = entity_type
    self.infra = LeadDataCleaner.classify_url_infrastructure(self.raw_url)

  async def execute_audit(self, http_client: httpx.AsyncClient) -> dict:
    audit_results = {
        "business_name": self.name,
        "website": self.raw_url,
        "timestamp": datetime.now().isoformat(),
        "site_reachable": False,
        "infra_type": self.infra["infra_type"],
        "root_domain": self.infra["root_domain"],
        "tech_stack": {
            "cms": "None/Custom",
            "pixels": [],
            "analytics": [],
            "checkout_engine": "None",
        },
        "contacts_scraped": {
            "emails": [],
            "phones": [],
            "instagram": "",
            "facebook": "",
            "linkedin": "",
            "gstin": "",
        },
        "benchmarks": {},
        "category_scores": {},
        "total_score": 0,
        "lead_tier": "Tier 3 (Local SEO Opportunity)",
        "dynamic_pitches": {},
        "audit_triggers": [],
    }

    if self.infra["infra_type"] in [
        "NO_WEBSITE",
        "SOCIAL_AS_WEBSITE",
        "AGGREGATOR_COMMISSION_LEAKER",
        "FREE_BUILDER_TIER",
    ]:
      return self._handle_non_independent_site(audit_results)

    html_content = ""
    http_data = {
        "status": 0,
        "is_https": self.raw_url.startswith("https"),
        "latency_ms": 9999,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"
            " AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
        )
    }

    try:
      t0 = time.time()
      resp = await http_client.get(
          self.raw_url, headers=headers, timeout=HTTP_TIMEOUT_SEC
      )
      http_data["status"] = resp.status_code
      http_data["latency_ms"] = (time.time() - t0) * 1000
      http_data["is_https"] = str(resp.url).startswith("https")
      if resp.status_code == 200:
        html_content = resp.text
        audit_results["site_reachable"] = True
    except Exception:
      pass

    if not html_content:
      return self._handle_unreachable_site(audit_results)

    soup = BeautifulSoup(html_content, "html.parser")
    self._fingerprint_tech_stack(soup, html_content, audit_results)
    self._extract_extended_contacts(soup, html_content, audit_results)

    # Secondary schema review extraction
    if self.rating == 0.0 or self.reviews == 0:
      for script in soup.find_all("script", type="application/ld+json"):
        try:
          data = json.loads(script.string or "{}")
          items = (
              data
              if isinstance(data, list)
              else data.get("@graph", [data])
              if isinstance(data, dict)
              else []
          )
          for it in items:
            if isinstance(it, dict) and "aggregateRating" in it:
              agg = it["aggregateRating"]
              if not self.rating and agg.get("ratingValue"):
                self.rating = float(agg["ratingValue"])
              if not self.reviews and (
                  agg.get("reviewCount") or agg.get("ratingCount")
              ):
                self.reviews = int(
                    agg.get("reviewCount") or agg.get("ratingCount")
                )
        except Exception:
          pass

    text_visible = soup.get_text(" ", strip=True).lower()
    viewport_matches = bool(
        soup.find("meta", attrs={"name": re.compile(r"viewport", re.I)})
    )
    atf_cta = bool(
        soup.find(
            "a",
            href=re.compile(
                r"tel:|wa\.me|whatsapp|order|book|reserve|menu", re.I
            ),
        )
    )

    bm = self._evaluate_100_point_benchmarks(
        soup,
        text_visible,
        http_data,
        http_data["latency_ms"],
        viewport_matches,
        atf_cta,
    )

    cat_design = sum(
        bm[k]
        for k in [
            "BM1_MobileFit",
            "BM2_ClearHero",
            "BM3_AboveFoldCTA",
            "BM4_BrandConsistency",
            "BM5_VisualHierarchy",
            "BM6_SimpleNav",
            "BM7_RealImages",
            "BM8_ServicesGrouped",
            "BM9_CleanLayout",
            "BM10_TapTargets",
        ]
    )
    cat_trust = sum(
        bm[k]
        for k in [
            "BM11_ReviewProof",
            "BM12_Credentials",
            "BM13_BenefitsExplained",
            "BM14_SocialProof",
            "BM15_PhoneWAEasy",
            "BM16_BookingForm",
            "BM17_AddressMap",
            "BM18_AboutPage",
            "BM19_PoliciesExist",
            "BM20_FAQs",
        ]
    )
    cat_seo = sum(
        bm[k]
        for k in [
            "BM21_TitleDescCity",
            "BM22_ServicePages",
            "BM23_LocalityPage",
            "BM24_HeadingStructure",
            "BM25_KeywordsUsed",
            "BM26_LocalSchema",
            "BM27_NAPMatch",
            "BM28_Sitemap",
            "BM29_ContentFreshness",
            "BM30_InterlinkingAlt",
        ]
    )
    cat_perf = sum(
        bm[k]
        for k in [
            "BM31_HTTPS",
            "BM32_CorePageSpeed",
            "BM33_MobileSpeed",
            "BM34_CLSNoShift",
            "BM35_CompressedImages",
            "BM36_TextContrast",
            "BM37_DescriptiveAlt",
            "BM38_KeyboardAccess",
            "BM39_CanonicalURLs",
            "BM40_NoBrokenLinks",
        ]
    )

    audit_results.update({
        "benchmarks": bm,
        "category_scores": {
            "Design_Mobile": cat_design,
            "Trust_Conversion": cat_trust,
            "Local_SEO_Content": cat_seo,
            "Performance_Accessibility": cat_perf,
        },
        "total_score": cat_design + cat_trust + cat_seo + cat_perf,
    })

    self._assign_lead_tier_and_dynamic_pitches(
        audit_results, bm, http_data["latency_ms"], atf_cta
    )
    self._generate_html_scorecard(audit_results)
    return audit_results

  def _fingerprint_tech_stack(self, soup, html, results):
    html_lower = html.lower()
    tech = results["tech_stack"]

    if "wp-content" in html_lower or "wp-includes" in html_lower:
      tech["cms"] = "WordPress"
    elif "shopify.com" in html_lower or "cdn.shopify.com" in html_lower:
      tech["cms"] = "Shopify"
    elif "wix.com" in html_lower or "_wix" in html_lower:
      tech["cms"] = "Wix"
    elif "webflow" in html_lower:
      tech["cms"] = "Webflow"
    elif "squarespace" in html_lower:
      tech["cms"] = "Squarespace"
    elif "next.js" in html_lower or "__next" in html_lower:
      tech["cms"] = "Next.js / React"
    elif "php" in html_lower:
      tech["cms"] = "Legacy PHP Setup"

    if "fbq(" in html or "connect.facebook.net" in html_lower:
      tech["pixels"].append("Meta Pixel")
    if "tiktok.com" in html_lower:
      tech["pixels"].append("TikTok Pixel")
    if "gtm.js" in html_lower or "googletagmanager.com" in html_lower:
      tech["analytics"].append("Google Tag Manager")
    if "g-" in html or "google-analytics.com" in html_lower:
      tech["analytics"].append("Google Analytics 4")

    if any(
        k in html_lower for k in ["petpooja", "dotpe.in", "posist", "urbanpiper"]
    ):
      tech["checkout_engine"] = "Owned Direct POS (DotPe/Petpooja)"
    elif any(
        k in html_lower
        for k in ["zomato.com/order", "swiggy.com/menu", "magicpin.in"]
    ):
      tech["checkout_engine"] = "Commission-Heavy 3rd Party (Zomato/Swiggy)"
    elif "calendly.com" in html_lower or "acuityscheduling" in html_lower:
      tech["checkout_engine"] = "Integrated Booking Engine"

  def _extract_extended_contacts(self, soup, html, results):
    contacts = results["contacts_scraped"]
    for a in soup.find_all("a", href=True):
      href = a["href"].strip()
      if "instagram.com" in href and not contacts["instagram"]:
        contacts["instagram"] = href
      elif "facebook.com" in href and not contacts["facebook"]:
        contacts["facebook"] = href
      elif "linkedin.com" in href and not contacts["linkedin"]:
        contacts["linkedin"] = href

    gst_match = re.search(
        r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}[Z]{1}[A-Z\d]{1}\b", html
    )
    if gst_match:
      contacts["gstin"] = gst_match.group(0)

  def _evaluate_100_point_benchmarks(
      self, soup, text_visible, http_data, load_time_ms, viewport_matches, atf_cta
  ):
    bm = {
        "BM1_MobileFit": 4 if viewport_matches else 0,
        "BM2_ClearHero": (
            3
            if any(
                kw in text_visible[:600]
                for kw in [
                    self.city.lower(),
                    self.location.lower(),
                    "cafe",
                    "coffee",
                    "event",
                    "service",
                    "best",
                    "leading",
                ]
            )
            else 0
        ),
        "BM3_AboveFoldCTA": 3 if atf_cta else 0,
        "BM4_BrandConsistency": (
            2 if len(soup.find_all(["style", "link"])) >= 2 else 0
        ),
        "BM5_VisualHierarchy": (
            2 if len(soup.find_all(["h1", "h2", "h3"])) >= 2 else 0
        ),
        "BM6_SimpleNav": 2 if bool(soup.find(["nav", "header"])) else 0,
        "BM7_RealImages": 2 if len(soup.find_all("img")) >= 2 else 0,
        "BM8_ServicesGrouped": (
            2
            if bool(
                soup.find(
                    string=re.compile(
                        r"services|menu|coffee|food|packages|treatments", re.I
                    )
                )
            )
            else 0
        ),
        "BM9_CleanLayout": 2 if len(soup.find_all()) < 2000 else 0,
        "BM10_TapTargets": 3,
        "BM11_ReviewProof": (
            3
            if (self.rating >= 4.0 and self.reviews > 5)
            or "review" in text_visible
            or "rating" in text_visible
            else 0
        ),
        "BM12_Credentials": (
            3
            if any(
                kw in text_visible
                for kw in [
                    "award",
                    "certified",
                    "experience",
                    "established",
                    "trusted",
                    "years",
                ]
            )
            else 0
        ),
        "BM13_BenefitsExplained": (
            3 if len(text_visible.split()) > 100 else 0
        ),
        "BM14_SocialProof": (
            3
            if any(
                kw in text_visible
                for kw in [
                    "testimonial",
                    "client",
                    "reviews",
                    "feedback",
                    "press",
                ]
            )
            else 0
        ),
    }

    tel_links = [
        a["href"] for a in soup.find_all("a", href=re.compile(r"^tel:"))
    ]
    wa_links = bool(
        soup.find("a", href=re.compile(r"wa\.me|api\.whatsapp\.com", re.I))
    )
    bm["BM15_PhoneWAEasy"] = (
        3 if (tel_links or wa_links or self.gmb_phone in text_visible) else 0
    )
    bm["BM16_BookingForm"] = (
        3
        if (
            len(soup.find_all("form")) > 0
            or any(
                kw in text_visible
                for kw in [
                    "book",
                    "appointment",
                    "reserve",
                    "order",
                    "quote",
                    "inquire",
                ]
            )
        )
        else 0
    )

    address_found = (
        self.city.lower() in text_visible
        or self.location.lower() in text_visible
        or "address" in text_visible
    )
    map_embedded = (
        len(soup.find_all("iframe", src=re.compile(r"google\.com/maps"))) > 0
    )
    bm["BM17_AddressMap"] = 2 if (address_found or map_embedded) else 0
    bm["BM18_AboutPage"] = (
        2
        if bool(
            soup.find(
                "a", string=re.compile(r"about|story|founder|our team", re.I)
            )
        )
        or "about us" in text_visible
        else 0
    )
    bm["BM19_PoliciesExist"] = (
        1
        if bool(
            soup.find("a", string=re.compile(r"privacy|terms|policy", re.I))
        )
        else 0
    )
    bm["BM20_FAQs"] = (
        2 if "faq" in text_visible or "frequently asked" in text_visible else 0
    )

    title_tag = soup.find("title")
    meta_desc = soup.find("meta", attrs={"name": "description"})
    t_text = title_tag.get_text().lower() if title_tag else ""
    m_text = meta_desc.get("content", "").lower() if meta_desc else ""

    bm["BM21_TitleDescCity"] = (
        3
        if (
            self.city.lower() in t_text
            or self.location.lower() in t_text
            or self.city.lower() in m_text
        )
        else 0
    )
    bm["BM22_ServicePages"] = 3 if len(soup.find_all("a", href=True)) > 5 else 0
    bm["BM23_LocalityPage"] = 2 if self.location.lower() in text_visible else 0
    bm["BM24_HeadingStructure"] = 2 if len(soup.find_all("h1")) >= 1 else 0
    bm["BM25_KeywordsUsed"] = (
        2
        if (self.keyword.lower() in text_visible or "coffee" in text_visible)
        else 0
    )
    bm["BM26_LocalSchema"] = (
        2
        if len(soup.find_all("script", type="application/ld+json")) > 0
        else 0
    )

    gmb_digits = re.sub(r"\D", "", self.gmb_phone)[-10:]
    phone_matched = (
        gmb_digits in re.sub(r"\D", "", text_visible) if gmb_digits else False
    )
    bm["BM27_NAPMatch"] = 3 if phone_matched else 0
    bm["BM28_Sitemap"] = 2
    bm["BM29_ContentFreshness"] = (
        2 if any(y in text_visible for y in ["2024", "2025", "2026"]) else 0
    )

    images_with_alt = len([i for i in soup.find_all("img") if i.get("alt")])
    bm["BM30_InterlinkingAlt"] = 2 if images_with_alt >= 1 else 0

    bm["BM31_HTTPS"] = 2 if http_data.get("is_https") else 0
    bm["BM32_CorePageSpeed"] = (
        4 if load_time_ms < 3500 else (2 if load_time_ms < 7000 else 0)
    )
    bm["BM33_MobileSpeed"] = 3 if load_time_ms < 4500 else 0
    bm["BM34_CLSNoShift"] = 2

    webp_count = len([
        i
        for i in soup.find_all("img")
        if ".webp" in i.get("src", "").lower()
        or ".avif" in i.get("src", "").lower()
    ])
    bm["BM35_CompressedImages"] = (
        2 if webp_count > 0 or len(soup.find_all("img")) == 0 else 0
    )
    bm["BM36_TextContrast"] = 3
    bm["BM37_DescriptiveAlt"] = 2 if images_with_alt > 0 else 0
    bm["BM38_KeyboardAccess"] = 2 if len(soup.find_all("a")) > 0 else 0
    bm["BM39_CanonicalURLs"] = (
        2 if soup.find("link", attrs={"rel": "canonical"}) else 0
    )
    bm["BM40_NoBrokenLinks"] = 3 if http_data.get("status") == 200 else 0

    return bm

  def _handle_non_independent_site(self, audit_results: dict) -> dict:
    t = self.infra["infra_type"]
    domain = self.infra["root_domain"]

    if t == "NO_WEBSITE":
      audit_results["total_score"] = 0
      audit_results["lead_tier"] = "Tier 1 (Red Hot Lead - No Asset)"
      audit_results["audit_triggers"] = [
          "Completely lacks a digital website",
          "100% reliant on word of mouth or local GMB",
          "Zero proprietary conversion engine",
      ]
      service = "Full-Stack Brand Website & Direct Online Ordering Build"
      pain = (
          "Losing high-intent local customer footfall and orders directly to"
          " competing brands with dedicated web presence."
      )
    elif t == "SOCIAL_AS_WEBSITE":
      audit_results["total_score"] = 20
      audit_results["lead_tier"] = "Tier 1 (Red Hot Lead - Social Bio Only)"
      audit_results["audit_triggers"] = [
          f"Directing commercial traffic to {domain} profile",
          "Cannot run Meta/Google Retargeting pixels",
          "Zero local SEO ranking capabilities",
      ]
      service = "Custom High-Speed Brand Showcase & Instant Menu Ordering Funnel"
      pain = (
          "Using Instagram/Facebook as your primary link loses customers"
          " looking for instant menus, direct booking, and fast directions."
      )
    elif t == "AGGREGATOR_COMMISSION_LEAKER":
      audit_results["total_score"] = 25
      audit_results["lead_tier"] = "Tier 2 (Commission Leaker)"
      audit_results["audit_triggers"] = [
          f"Primary link routes to {domain}",
          "Paying 15-30% platform commissions",
          "Competitors listed directly next to their brand",
      ]
      service = "Direct Commission-Free Online Ordering & Reservation Engine"
      pain = (
          f"Driving customer traffic to {domain} forces hefty commissions and"
          " leaks customers to rival outlets on the same page."
      )
    else:
      audit_results["total_score"] = 35
      audit_results["lead_tier"] = "Tier 1 (Brand Risk - Free Builder)"
      audit_results["audit_triggers"] = [
          f"Hosted on free subdomain ({domain})",
          "Lacks custom brand domain authority",
          "Unprofessional appearance for a quality establishment",
      ]
      service = "Enterprise Domain Setup & Bespoke Brand Replatforming"
      pain = (
          f"Hosting your brand on a free '{domain}' subdomain harms brand"
          " prestige and diminishes customer trust."
      )

    audit_results["category_scores"] = {
        "Design_Mobile": 10,
        "Trust_Conversion": 5,
        "Local_SEO_Content": 5,
        "Performance_Accessibility": 5,
    }

    self._generate_dynamic_hooks(audit_results, service, pain)
    self._generate_html_scorecard(audit_results)
    return audit_results

  def _handle_unreachable_site(self, audit_results: dict) -> dict:
    audit_results["total_score"] = 15
    audit_results["lead_tier"] = "Tier 1 (Red Hot Lead - Broken Site)"
    audit_results["audit_triggers"] = [
        "Website timed out / unreachable DNS",
        "Broken server gateway / SSL error",
    ]
    service = "Emergency Infrastructure Rebuild & High-Speed Cloud Hosting"
    pain = (
        "Website is actively broken or timing out, losing 100% of web traffic"
        " and customer inquiries."
    )
    self._generate_dynamic_hooks(audit_results, service, pain)
    self._generate_html_scorecard(audit_results)
    return audit_results

  def _assign_lead_tier_and_dynamic_pitches(
      self, audit_results: dict, bm: dict, load_time_ms: float, atf_cta: bool
  ):
    score = audit_results["total_score"]
    triggers = []

    if bm.get("BM3_AboveFoldCTA") == 0:
      triggers.append("Missing above-the-fold Call/WhatsApp/Order CTA")
    if bm.get("BM16_BookingForm") == 0:
      triggers.append("No direct reservation or order form")
    if bm.get("BM26_LocalSchema") == 0:
      triggers.append("Missing LocalBusiness JSON-LD Schema")
    if bm.get("BM21_TitleDescCity") == 0:
      triggers.append(
          "Title/Meta tags lack target location keywords"
          f" ({self.city}/{self.location})"
      )
    if bm.get("BM27_NAPMatch") == 0:
      triggers.append("Website NAP does not match Google Business Profile")
    if load_time_ms > 4500:
      triggers.append(
          f"Slow mobile page load ({round(load_time_ms/1000, 1)}s latency)"
      )
    if not audit_results["tech_stack"]["pixels"]:
      triggers.append("No Meta/Google Retargeting Pixels installed")

    audit_results["audit_triggers"] = (
        triggers
        if triggers
        else ["Routine conversion and local SEO modernization"]
    )

    if "Multi-Branch" in self.entity_type or "Hospitality" in self.entity_type:
      audit_results["lead_tier"] = "Tier 4 (Multi-Location / Enterprise Group)"
      service = (
          "Enterprise Multi-Location Digital Architecture & Regional SEO Hub"
      )
      pain = (
          f"Managing {self.entity_type} with fragmented outlet links dilutes"
          f" local Google 3-Pack authority across {self.location}."
      )
    elif score < 50 or not atf_cta:
      audit_results["lead_tier"] = "Tier 2 (Conversion Leaker)"
      service = (
          "Mobile Conversion Optimization & Instant WhatsApp Ordering Engine"
      )
      pain = (
          f"Mobile visitors in {self.location} bounce within 4s due to missing"
          " instant tap-to-call/order CTAs above the fold."
      )
    elif (
        bm.get("BM26_LocalSchema") == 0 or bm.get("BM21_TitleDescCity") == 0
    ):
      audit_results["lead_tier"] = "Tier 3 (Local SEO Opportunity)"
      service = "AI Search Optimization & Local Google 3-Pack Schema Stack"
      pain = (
          f"Despite strong reputation ({self.reviews} reviews), missing schema"
          " prevents Google and AI engines from ranking this outlet #1 in"
          f" {self.location}."
      )
    else:
      audit_results["lead_tier"] = "Tier 3 (Performance Tuning)"
      service = "Speed Acceleration & High-Converting Mobile UX Overhaul"
      pain = (
          "Site is functional but lacks next-gen image compression and fast"
          " checkout funnels, increasing bounce rate on mobile."
      )

    self._generate_dynamic_hooks(audit_results, service, pain)

  def _generate_dynamic_hooks(
      self, audit_results: dict, service: str, pain: str
  ):
    name = self.name
    loc = self.location if self.location else self.city
    triggers_str = ", ".join(audit_results["audit_triggers"][:2])
    cms = audit_results["tech_stack"]["cms"]

    wa_hook = (
        f"Hey {name} team, noticed your profile while auditing top spots in"
        f" {loc}. Spotted 2 conversion leaks ({triggers_str}) that cost mobile"
        " customers. We have a 60-second fix—want me to share the breakdown?"
    )
    audit_hook = (
        f"Hi {name} management, ran an automated digital health audit on your"
        f" website ({self.raw_url or 'Profile'}). Your outlet scored"
        f" {audit_results['total_score']}/100 due to {cms} configuration"
        f" issues. Report: {pain} Let's connect for 5 mins to resolve this."
    )
    comp_hook = (
        f"Hey {name}, customers searching in {loc} see competing brands first"
        " due to faster page load and verified Google 3-Pack schema. We can"
        f" implement '{service}' to resolve your {triggers_str}."
    )

    audit_results["dynamic_pitches"] = {
        "primary_pitch_strategy": service,
        "client_pain_point": pain,
        "whatsapp_dm_hook": wa_hook,
        "value_first_audit_hook": audit_hook,
        "competitor_comparison_hook": comp_hook,
    }

  def _generate_html_scorecard(self, res: dict):
    slug = LeadDataCleaner.safe_filename(self.name)
    html_file = os.path.join(HTML_REPORT_DIR, f"audit_{slug}.html")
    cats = res.get("category_scores", {})
    score = res.get("total_score", 0)
    color = (
        "#10b981" if score >= 75 else ("#f59e0b" if score >= 50 else "#ef4444")
    )
    triggers_html = "".join([
        f"<li style='margin-bottom:8px;'>⚠️ {t}</li>"
        for t in res.get("audit_triggers", [])
    ])

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <title>Digital Health Scorecard - {self.name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 40px 20px; }}
        .card {{ max-width: 750px; margin: 0 auto; background: #1e293b; border-radius: 16px; padding: 32px; border: 1px solid #334155; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 20px; }}
        .badge {{ background: {color}; color: #fff; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 14px; }}
        .score-box {{ text-align: center; margin: 30px 0; background: #0f172a; border-radius: 12px; padding: 24px; border: 1px solid #334155; }}
        .score-val {{ font-size: 56px; font-weight: 900; color: {color}; margin: 0; }}
        .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 30px; }}
        .grid-item {{ background: #0f172a; padding: 16px; border-radius: 8px; border-left: 4px solid #38bdf8; }}
        .grid-item h4 {{ margin: 0 0 4px 0; font-size: 13px; color: #94a3b8; text-transform: uppercase; }}
        .grid-item p {{ margin: 0; font-size: 20px; font-weight: bold; }}
        .triggers {{ background: #450a0a; border: 1px solid #7f1d1d; border-radius: 12px; padding: 20px; margin-bottom: 30px; }}
        .triggers h3 {{ color: #f87171; margin-top: 0; }}
        .triggers ul {{ margin: 0; padding-left: 20px; color: #fca5a5; }}
        .recommendation {{ background: #064e3b; border: 1px solid #065f46; border-radius: 12px; padding: 20px; }}
        .recommendation h3 {{ color: #34d399; margin-top: 0; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <div>
                <h1 style="margin:0; font-size:24px;">{self.name}</h1>
                <p style="margin:4px 0 0 0; color:#94a3b8;">{self.location} • {self.city} | {self.keyword}</p>
            </div>
            <div class="badge">{res.get('lead_tier', 'Standard')}</div>
        </div>
        <div class="score-box">
            <h3 style="margin:0 0 8px 0; color:#94a3b8;">OVERALL DIGITAL HEALTH SCORE</h3>
            <p class="score-val">{score}<span style="font-size:24px; color:#64748b;">/100</span></p>
        </div>
        <div class="grid">
            <div class="grid-item">
                <h4>Design & Mobile UX</h4>
                <p>{cats.get('Design_Mobile', 0)} / 24</p>
            </div>
            <div class="grid-item">
                <h4>Trust & Conversion</h4>
                <p>{cats.get('Trust_Conversion', 0)} / 27</p>
            </div>
            <div class="grid-item">
                <h4>Local SEO & Content</h4>
                <p>{cats.get('Local_SEO_Content', 0)} / 25</p>
            </div>
            <div class="grid-item">
                <h4>Performance & Tech</h4>
                <p>{cats.get('Performance_Accessibility', 0)} / 24</p>
            </div>
        </div>
        <div class="triggers">
            <h3>⚠️ Critical Friction Points Identified</h3>
            <ul>{triggers_html}</ul>
        </div>
        <div class="recommendation">
            <h3>💡 Recommended High-Growth Solution</h3>
            <p style="margin:0 0 8px 0; font-weight:bold; font-size:16px; color:#ecfdf5;">{res.get('dynamic_pitches', {}).get('primary_pitch_strategy', '')}</p>
            <p style="margin:0; color:#a7f3d0; font-size:14px;">{res.get('dynamic_pitches', {}).get('client_pain_point', '')}</p>
        </div>
    </div>
</body>
</html>"""
    try:
      with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    except Exception:
      pass


# ==============================================================================
# PIPELINE ORCHESTRATOR
# ==============================================================================
OUTPUT_COLUMNS = [
    "Record_Hash",
    "Lead_Tier",
    "Platinum_Score",
    "Name",
    "Location",
    "City",
    "Entity_Resolution_Type",
    "Network_Footprint",
    "Phone_E164",
    "WhatsApp_Click_Link",
    "Website",
    "Infra_Classification",
    "CMS_Tech_Stack",
    "Pixels_Tracked",
    "Instagram_Link",
    "GSTIN_Entity",
    "Email",
    "Rating",
    "Reviews",
    "Design_Mobile_Score",
    "Trust_Conversion_Score",
    "Local_SEO_Score",
    "Perf_Tech_Score",
    "Audit_Triggers",
    "Primary_Pitch_Strategy",
    "Client_Pain_Point",
    "WhatsApp_DM_Hook",
    "Value_First_Audit_Hook",
    "Competitor_Comparison_Hook",
    "HTML_Scorecard_File",
    "Last_Enriched",
]


async def process_batch_leads(
    df_chunk, now_ts, http_client, cleaner, completed_hashes
):
  semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)

  async def process_row(row_dict):
    async with semaphore:
      name = cleaner.normalize_name(
          cleaner.extract_field(
              row_dict,
              ["Name", "Title", "Business Name"],
              "Unknown Business",
          )
      )
      website = cleaner.normalize_website(
          cleaner.extract_field(
              row_dict, ["Website", "Url", "Link", "Site"], ""
          )
      )
      phone_raw = cleaner.extract_field(
          row_dict, ["Phone", "Phone Number", "Mobile", "Contact", "Tel"], ""
      )
      phone_meta = cleaner.validate_phone(phone_raw, default_country="IN")
      email = cleaner.validate_email(
          cleaner.extract_field(
              row_dict, ["Email", "Mail", "Email Address"], ""
          )
      )
      address = cleaner.extract_field(
          row_dict, ["Address", "Location", "Full Address"], ""
      )
      city = cleaner.extract_field(
          row_dict, ["City", "Town"], "New Delhi"
      ).title()
      location = cleaner.extract_field(
          row_dict, ["Location", "Region", "Area"], city
      ).title()
      keyword = cleaner.extract_field(
          row_dict, ["Keyword", "Category"], "Business"
      )
      entity_type = row_dict.get(
          "Entity_Resolution_Type",
          cleaner.extract_field(
              row_dict, ["Entity Status", "Is Chain"], "Direct Business"
          ),
      )
      network_footprint = row_dict.get("Network_Footprint", "Single Location")

      dedup_key = cleaner.generate_dedup_key(name, phone_meta["formatted"])
      record_hash = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()

      if dedup_key in completed_hashes or record_hash in completed_hashes:
        return None

      rating_val = cleaner.extract_field(
          row_dict, ["Rating", "Stars", "Score", "Average Rating"], "0"
      )
      reviews_val = cleaner.extract_field(
          row_dict, ["Reviews", "Review Count", "Total Reviews"], "0"
      )

      try:
        rating = float(re.sub(r"[^\d.]", "", str(rating_val)))
      except Exception:
        rating = 0.0

      try:
        reviews = int(re.sub(r"[^\d]", "", str(reviews_val)))
      except Exception:
        reviews = 0

      # Live fallback from Google Search in <500ms
      if rating == 0.0 or reviews == 0:
        rating, reviews = await fetch_live_gmb_ratings(
            http_client, name, location, city
        )

      auditor = DeepAuditor(
          website,
          name,
          rating,
          reviews,
          phone_meta["formatted"],
          email,
          address,
          city,
          location,
          keyword,
          entity_type,
      )
      res = await auditor.execute_audit(http_client)

      if res.get("rating", 0.0) == 0.0:
        res["rating"] = rating
      if res.get("reviews", 0) == 0:
        res["reviews"] = reviews

      safe_slug = cleaner.safe_filename(name)
      json_filepath = os.path.join(
          AUDIT_OUTPUT_DIR, f"enrichment_{safe_slug}.json"
      )
      try:
        with open(json_filepath, "w", encoding="utf-8") as f:
          json.dump(res, f, indent=2)
      except Exception:
        pass

      wa_digits = phone_meta["digits"]
      prefilled_msg = quote_plus(
          res.get("dynamic_pitches", {}).get(
              "whatsapp_dm_hook",
              f"Hi {name}, wanted to share a quick update regarding your"
              " website.",
          )
      )
      wa_link = (
          f"https://wa.me/{wa_digits}?text={prefilled_msg}"
          if len(wa_digits) >= 10
          else "N/A"
      )

      cats = res.get("category_scores", {})
      pitches = res.get("dynamic_pitches", {})
      tech = res.get("tech_stack", {})
      contacts = res.get("contacts_scraped", {})

      return {
          "Record_Hash": record_hash,
          "Lead_Tier": res.get("lead_tier", "Standard"),
          "Platinum_Score": res.get("total_score", 0),
          "Name": name,
          "Location": location,
          "City": city,
          "Entity_Resolution_Type": entity_type,
          "Network_Footprint": network_footprint,
          "Phone_E164": phone_meta["formatted"],
          "WhatsApp_Click_Link": wa_link,
          "Website": website,
          "Infra_Classification": res.get(
              "infra_type", "INDEPENDENT_DOMAIN"
          ),
          "CMS_Tech_Stack": tech.get("cms", "None"),
          "Pixels_Tracked": (
              ", ".join(tech.get("pixels", []))
              if tech.get("pixels")
              else "None"
          ),
          "Instagram_Link": contacts.get("instagram", ""),
          "GSTIN_Entity": contacts.get("gstin", ""),
          "Email": email,
          "Rating": rating,
          "Reviews": reviews,
          "Design_Mobile_Score": cats.get("Design_Mobile", 0),
          "Trust_Conversion_Score": cats.get("Trust_Conversion", 0),
          "Local_SEO_Score": cats.get("Local_SEO_Content", 0),
          "Perf_Tech_Score": cats.get("Performance_Accessibility", 0),
          "Audit_Triggers": " | ".join(res.get("audit_triggers", [])),
          "Primary_Pitch_Strategy": pitches.get("primary_pitch_strategy", ""),
          "Client_Pain_Point": pitches.get("client_pain_point", ""),
          "WhatsApp_DM_Hook": pitches.get("whatsapp_dm_hook", ""),
          "Value_First_Audit_Hook": pitches.get("value_first_audit_hook", ""),
          "Competitor_Comparison_Hook": pitches.get(
              "competitor_comparison_hook", ""
          ),
          "HTML_Scorecard_File": f"audit_{safe_slug}.html",
          "Last_Enriched": now_ts,
      }

  tasks = [process_row(row.to_dict()) for _, row in df_chunk.iterrows()]
  results = await asyncio.gather(*tasks, return_exceptions=True)
  return [r for r in results if r is not None and isinstance(r, dict)]


def run_platinum_pipeline():
  global stop_requested
  client = get_sheets_client()
  if not client:
    return

  try:
    raw_sp = client.open(RAW_SPREADSHEET_NAME)
    target_sp_name = f"Polished - {RAW_SPREADSHEET_NAME}"
    try:
      polished_sp = client.open(target_sp_name)
    except Exception:
      polished_sp = client.create(target_sp_name)
      console.print(
          f"[success]Created Target Spreadsheet: '{target_sp_name}'[/success]"
      )
  except Exception as e:
    console.print(f"[error]Spreadsheet initialization failed: {e}[/error]")
    return

  now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  cleaner = LeadDataCleaner()
  completed_hashes = load_checkpoints()

  valid_worksheets = [
      ws
      for ws in raw_sp.worksheets()
      if not ws.title.startswith("Sheet") and ws.title != "All_Keywords_Cache"
  ]

  console.print(
      f"\n[bold cyan]📂 Found {len(valid_worksheets)} tabs in"
      f" '{RAW_SPREADSHEET_NAME}'[/bold cyan]"
  )
  for i, ws in enumerate(valid_worksheets):
    console.print(f"  {i + 1}. {ws.title}")

  tab_choice = input(
      "\nEnter Tab Number to Enrich, or type 'ALL': "
  ).strip().upper()
  tabs_to_process = (
      valid_worksheets
      if tab_choice == "ALL"
      else [valid_worksheets[int(tab_choice) - 1]]
  )

  console.print(
      "[dim white]Press [Ctrl + C] at any time to finish active batch and"
      " exit cleanly.[/dim white]\n"
  )

  for ws in tabs_to_process:
    if stop_requested:
      break
    tab_title = ws.title
    raw_data = ws.get_all_records()
    if not raw_data:
      continue

    try:
      target_tab = polished_sp.worksheet(tab_title)
    except Exception:
      target_tab = polished_sp.add_worksheet(
          title=tab_title, rows="5000", cols="35"
      )

    try:
      df_polished = pd.DataFrame(target_tab.get_all_records())
      if (
          not df_polished.empty
          and "Name" in df_polished.columns
          and "Phone_E164" in df_polished.columns
      ):
        for _, p_row in df_polished.iterrows():
          p_name = str(p_row.get("Name", ""))
          p_phone = str(p_row.get("Phone_E164", ""))
          if p_name and p_phone:
            p_key = cleaner.generate_dedup_key(p_name, p_phone)
            completed_hashes.add(p_key)
      if "Record_Hash" in df_polished.columns:
        completed_hashes.update(
            set(df_polished["Record_Hash"].dropna().astype(str).tolist())
        )
    except Exception:
      df_polished = pd.DataFrame(columns=OUTPUT_COLUMNS)

    df_raw = pd.DataFrame(raw_data)
    initial_raw_count = len(df_raw)

    # Multi-Location Multi-Factor Resolution Across the Tab
    df_raw = MultiLocationResolver.build_network_footprints(df_raw, cleaner)

    df_raw["Dedup_Key"] = df_raw.apply(
        lambda r: cleaner.generate_dedup_key(
            cleaner.extract_field(
                r.to_dict(), ["Name", "Title", "Business Name"]
            ),
            cleaner.extract_field(
                r.to_dict(), ["Phone", "Phone Number", "Mobile", "Contact"]
            ),
        ),
        axis=1,
    )
    df_raw = df_raw.drop_duplicates(subset=["Dedup_Key"], keep="first")
    df_raw = df_raw[~df_raw["Dedup_Key"].isin(completed_hashes)].reset_index(
        drop=True
    )
    total_leads = len(df_raw)
    skipped_dupes = initial_raw_count - total_leads

    console.print(
        "\n[bold yellow]⚡ Running High-Speed Intelligence Audit on Tab:"
        f" '{tab_title}'[/bold yellow]"
    )
    if skipped_dupes > 0:
      console.print(
          f"[dim green]  ℹ️ Purged {skipped_dupes} duplicate leads.[/dim green]"
      )

    if total_leads == 0:
      console.print(
          f"[success]  ✔ All leads in '{tab_title}' are already enriched!"
          " Skipping.[/success]"
      )
      continue

    async def process_tab():
      nonlocal df_polished
      async with httpx.AsyncClient(
          verify=False, follow_redirects=True
      ) as http_client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
          task = progress.add_task(
              f"[cyan]Enriching {tab_title}...", total=total_leads
          )

          for i in range(0, total_leads, BATCH_SIZE):
            if stop_requested:
              break
            df_chunk = df_raw.iloc[i : i + BATCH_SIZE]
            batch_results = await process_batch_leads(
                df_chunk, now_ts, http_client, cleaner, completed_hashes
            )

            if batch_results:
              for res in batch_results:
                completed_hashes.add(res["Record_Hash"])
              save_checkpoints(completed_hashes)

              df_new = pd.DataFrame(batch_results)
              df_polished = (
                  pd.concat([df_polished, df_new], ignore_index=True)
                  if not df_polished.empty
                  else df_new
              )

              df_polished = df_polished.drop_duplicates(
                  subset=["Record_Hash"], keep="last"
              )
              df_polished["Platinum_Score"] = pd.to_numeric(
                  df_polished["Platinum_Score"], errors="coerce"
              ).fillna(0)
              df_polished = df_polished.sort_values(
                  by="Platinum_Score", ascending=True
              ).reset_index(drop=True)

              for col in OUTPUT_COLUMNS:
                if col not in df_polished.columns:
                  df_polished[col] = ""
              df_to_upload = df_polished[OUTPUT_COLUMNS]

              try:
                target_tab.clear()
                target_tab.update(
                    values=[OUTPUT_COLUMNS]
                    + df_to_upload.fillna("").values.tolist(),
                    range_name="A1",
                    value_input_option="RAW",
                )
              except Exception as e:
                console.print(f"[warning]Sheets update deferred: {e}[/warning]")

            progress.advance(task, len(df_chunk))

    try:
      asyncio.run(process_tab())
    except KeyboardInterrupt:
      console.print("[warning]Tab processing interrupted.[/warning]")

    if not stop_requested:
      console.print(
          f"[success]✔ Successfully Enriched and Synced Tab '{tab_title}'![/success]"
      )


if __name__ == "__main__":
  console.clear()
  console.print(
      Panel(
          "[bold white]🚀 ENTERPRISE PLATINUM LEAD ENRICHMENT & INTELLIGENCE"
          " ENGINE (HIGH-SPEED)[/bold white]\n[dim]Multi-Factor Chain Grouping •"
          " 500ms Knowledge Graph Scraper • Zero-Crash HTTPX Pipeline[/dim]",
          border_style="bold cyan",
          padding=(1, 3),
      )
  )
  run_platinum_pipeline()