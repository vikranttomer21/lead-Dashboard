import urllib.parse
import re
from typing import Dict, Any, List, Tuple


class SalesIntelligenceEngine:
    """
    Translates raw technical audit data into simple, actionable, 
    and punchy sales cheat-sheets for outreach reps.
    """

    @classmethod
    def _parse_int(cls, val: Any, default: int = 0) -> int:
        if val is None or val == "":
            return default
        try:
            return int(float(str(val).strip()))
        except (ValueError, TypeError):
            return default

    @classmethod
    def _parse_str(cls, val: Any, default: str = "") -> str:
        if val is None:
            return default
        return str(val).strip()

    @classmethod
    def calculate_scores(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        raw_platinum = cls._parse_int(row.get("Platinum_Score"), 0)
        scraped_tier = cls._parse_str(row.get("Lead_Tier"), "")

        if raw_platinum == 0:
            opportunity_score = 95
        else:
            opportunity_score = max(10, 100 - raw_platinum)

        if opportunity_score >= 80:
            calculated_tier = "Tier 1 (High Priority Lead)"
            priority_level = "CRITICAL"
        elif opportunity_score >= 60:
            calculated_tier = "Tier 2 (Medium Opportunity)"
            priority_level = "ELEVATED"
        else:
            calculated_tier = "Tier 3 (Local SEO / Optimization)"
            priority_level = "STANDARD"

        return {
            "platinum_raw": raw_platinum,
            "opportunity_score": opportunity_score,
            "lead_tier": scraped_tier if scraped_tier else calculated_tier,
            "priority_level": priority_level,
        }

    @classmethod
    def _simplify_trigger_text(cls, trigger: str, business_name: str, location: str) -> str:
        """Converts raw technical audit text into clear sales-friendly bullet points."""
        t = trigger.lower().strip()

        if "instagram" in t or "social" in t or "facebook" in t or "linktr.ee" in t:
            return "Sending customers to Instagram/Social bio instead of an instant direct booking page."
        if "pixel" in t or "retarget" in t:
            return "Lost visitors: People who click their link and bounce can never be brought back with ads."
        if "no website" in t or "lacks a digital website" in t or "no asset" in t:
            return "No official website: 100% reliant on word-of-mouth or third-party apps."
        if "commission" in t or "zomato" in t or "swiggy" in t or "magicpin" in t:
            return "Paying heavy 15-30% aggregator commissions on orders they could capture directly."
        if "schema" in t or "json-ld" in t or "google 3-pack" in t or "local seo" in t:
            return f"Losing local Google rankings: Nearby competitors show up above them on Google Maps in {location}."
        if "timed out" in t or "ssl" in t or "broken" in t or "dns" in t:
            return "Website is down or showing security errors, losing customer trust immediately."
        if "slow" in t or "latency" in t:
            return "Slow mobile loading speed: Mobile users leave before the page even finishes loading."
        if "call" in t or "cta" in t or "above-the-fold" in t:
            return "No instant 'Call Now' or 'Order on WhatsApp' button visible when customers open the link."
        if "subdomain" in t or "grexa" in t or "wixsite" in t:
            return "Using an unbranded free web link, which looks unprofessional to high-paying customers."
        
        return trigger.strip()

    @classmethod
    def generate_sales_brief(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        business_name = cls._parse_str(row.get("Name"), "this business")
        city = cls._parse_str(row.get("City"), "your area")
        location = cls._parse_str(row.get("Location"), city)
        network = cls._parse_str(row.get("Network_Footprint"), "Single Location")
        entity_type = cls._parse_str(row.get("Entity_Resolution_Type"), "Independent")

        # Direct Scraped copy
        scraped_tier = cls._parse_str(row.get("Lead_Tier"), "Standard Digital Profile")
        scraped_triggers = cls._parse_str(row.get("Audit_Triggers"), "")
        scraped_pitch = cls._parse_str(row.get("Primary_Pitch_Strategy"), "")
        scraped_pain = cls._parse_str(row.get("Client_Pain_Point"), "")
        scraped_wa_hook = cls._parse_str(row.get("WhatsApp_DM_Hook"), "")

        # 1. Translate Pain Points / Leaks into Plain English
        simplified_pain_points = []
        if scraped_triggers:
            for raw_t in scraped_triggers.split("|"):
                if raw_t.strip():
                    simplified_pain_points.append(
                        cls._simplify_trigger_text(raw_t, business_name, location)
                    )
        elif scraped_pain:
            simplified_pain_points.append(cls._simplify_trigger_text(scraped_pain, business_name, location))
        else:
            simplified_pain_points.append(f"Customer drop-offs and missing instant booking buttons on mobile.")

        # 2. Translate What to Sell (Package Scope) into Clear Deliverables
        package_name = scraped_pitch or "Turnkey Direct-Customer Acquisition Hub"
        package_scope = [
            "⚡ Fast Mobile-Friendly Storefront (Loads in under 1 second)",
            "📲 Instant 1-Click WhatsApp Ordering & Table Reservation System",
            "📍 Google Maps & Local Search Setup (Rank #1 in your local area)",
            "⭐ Automated 5-Star Customer Review Collector"
        ]

        # 3. Simple Core Pitch Angle (What the salesperson should say)
        if "social" in scraped_tier.lower() or "instagram" in str(simplified_pain_points).lower():
            closer_hook = f"When customers look up {business_name}, sending them to an Instagram link loses orders. A dedicated 1-click WhatsApp storefront captures direct customers instantly without aggregator cuts."
        elif "broken" in scraped_tier.lower() or "broken" in str(simplified_pain_points).lower():
            closer_hook = f"{business_name}'s website is currently unreachable or slow. We can restore a lightning-fast cloud web platform in 24 hours so you never miss another customer inquiry."
        elif "commission" in scraped_tier.lower() or "aggregator" in str(simplified_pain_points).lower():
            closer_hook = f"{business_name} is sending its best direct traffic to food aggregators and paying 15–30% in fees. We install a zero-commission direct ordering system that keeps 100% of the profits."
        else:
            closer_hook = f"Position {business_name} as the top-ranking choice in {location} with automated booking and local Google Maps optimization."

        # 4. Realistic Everyday Sales Objections & Comebacks
        objections = [
            {
                "objection": "We already get enough business through word-of-mouth or Instagram.",
                "rebuttal": f"Word-of-mouth is great, but when those referrals search '{business_name}' on their phone to check menu prices or book a table, an Instagram bio link creates friction. A 1-click WhatsApp direct checkout converts them in 5 seconds before they go to a competitor."
            },
            {
                "objection": "We are already listed on Zomato / Swiggy / Google Maps.",
                "rebuttal": "Aggregators take up to 25–30% of your bill and list your competitors right next to your menu. Having your own direct link means zero commissions and you own the customer's phone number forever."
            },
            {
                "objection": "Send me the details on WhatsApp first.",
                "rebuttal": f"Sending the 60-second scorecard right away! Before I send it, are you looking primarily to cut third-party commission costs or get more direct table bookings in {location}?"
            }
        ]

        return {
            "category_title": scraped_tier,
            "pain_points": simplified_pain_points,
            "package_name": package_name,
            "package_scope": package_scope,
            "closer_hook": closer_hook,
            "objections": objections,
            "network_scope": f"{network} ({entity_type})",
            "whatsapp_dm_hook": scraped_wa_hook
        }

    @classmethod
    def get_outreach_pitch(
        cls, business_name: str, category: str, location: str, pitch_type: str = "GENERAL"
    ) -> Tuple[str, str, str]:
        biz = business_name or "there"
        cat = category or "outlet"
        loc = location or "your area"

        text = (
            f"Hey {biz} team, noticed your profile while auditing top {cat}s in {loc}. "
            f"Spotted a couple of quick conversion leaks on your mobile link that cost direct customer orders. "
            f"We have a 60-second fix that sets up instant direct WhatsApp ordering—open to taking a quick look?"
        )
        script = "Focus on direct WhatsApp bookings, zero commissions, and owning customer data."
        encoded = urllib.parse.quote(text)
        return text, script, encoded


# Module exports
calculate_scores = SalesIntelligenceEngine.calculate_scores
generate_sales_brief = SalesIntelligenceEngine.generate_sales_brief
get_outreach_pitch = SalesIntelligenceEngine.get_outreach_pitch