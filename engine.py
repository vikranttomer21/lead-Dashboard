import urllib.parse
from typing import Dict, Any, List, Tuple


class SalesIntelligenceEngine:
    """
    Enterprise sales analytics engine that translates technical gaps 
    into commercial value propositions, objection handling, and pitch packages.
    """

    WEIGHT_PLATINUM_DEFICIENCY = 0.50
    WEIGHT_INFRASTRUCTURE = 0.25
    WEIGHT_ANALYTICS_TRACKING = 0.15
    WEIGHT_NETWORK_FOOTPRINT = 0.10

    MODERN_STACKS = {"NEXTJS", "REACT", "SHOPIFY", "WEBFLOW", "GATSBY", "ASTRO", "WORDPRESS"}
    LEGACY_STACKS = {"WIX", "SQUARESPACE", "JOOMLA", "DRUPAL", "PHP", "STATIC HTML"}

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
        infra = cls._parse_str(row.get("Infra_Classification"), "").upper()
        website = cls._parse_str(row.get("Website"), "")
        tech_stack = cls._parse_str(row.get("CMS_Tech_Stack"), "").upper()
        pixels = cls._parse_str(row.get("Pixels_Tracked"), "").upper()
        network = cls._parse_str(row.get("Network_Footprint"), "").upper()

        platinum_deficiency = 100 - min(max(raw_platinum, 0), 100)

        if infra == "NO_WEBSITE" or not website or website.upper() in {"NONE", "NULL", "N/A", "NAN"}:
            infra_deficiency = 100
        elif any(legacy in tech_stack for legacy in cls.LEGACY_STACKS):
            infra_deficiency = 60
        elif any(modern in tech_stack for modern in cls.MODERN_STACKS):
            infra_deficiency = 20
        else:
            infra_deficiency = 50

        if pixels in {"NONE", "", "NULL", "FALSE", "NAN", "N/A"}:
            tracking_deficiency = 100
        else:
            tracking_deficiency = 20

        if "CHAIN" in network or "MULTI" in network:
            footprint_factor = 90
        elif "SINGLE" in network or "FLAGSHIP" in network:
            footprint_factor = 75
        else:
            footprint_factor = 60

        opportunity_score = round(
            (platinum_deficiency * cls.WEIGHT_PLATINUM_DEFICIENCY)
            + (infra_deficiency * cls.WEIGHT_INFRASTRUCTURE)
            + (tracking_deficiency * cls.WEIGHT_ANALYTICS_TRACKING)
            + (footprint_factor * cls.WEIGHT_NETWORK_FOOTPRINT)
        )
        opportunity_score = min(max(opportunity_score, 0), 100)

        if opportunity_score >= 85:
            lead_tier = "Tier 1 (High Opportunity)"
            priority_level = "CRITICAL"
        elif opportunity_score >= 70:
            lead_tier = "Tier 2 (Medium Opportunity)"
            priority_level = "ELEVATED"
        else:
            lead_tier = "Tier 3 (Standard)"
            priority_level = "STANDARD"

        return {
            "platinum_raw": raw_platinum,
            "opportunity_score": opportunity_score,
            "lead_tier": lead_tier,
            "priority_level": priority_level,
            "infra_deficiency": infra_deficiency,
            "tracking_deficiency": tracking_deficiency,
        }

    @classmethod
    def generate_sales_brief(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Synthesizes commercial intel, revenue leaks, customer pain points,
        recommended pricing/packages, and pitch angles for the salesperson.
        """
        infra = cls._parse_str(row.get("Infra_Classification"), "").upper()
        tech_stack_raw = cls._parse_str(row.get("CMS_Tech_Stack"), "None")
        pixels_raw = cls._parse_str(row.get("Pixels_Tracked"), "None")
        website = cls._parse_str(row.get("Website"), "")
        business_name = cls._parse_str(row.get("Name"), "this business")
        city = cls._parse_str(row.get("City"), "your area")
        location = cls._parse_str(row.get("Location"), city)
        network = cls._parse_str(row.get("Network_Footprint"), "Single Location")
        entity_type = cls._parse_str(row.get("Entity_Resolution_Type"), "Independent")

        # Normalize comparisons to uppercase
        pixels_upper = pixels_raw.upper()
        tech_stack_upper = tech_stack_raw.upper()
        website_upper = website.upper()

        has_no_website = (
            infra == "NO_WEBSITE" 
            or not website 
            or website_upper in {"NONE", "NULL", "N/A", "NAN"}
        )
        has_no_pixels = pixels_upper in {"NONE", "", "NULL", "FALSE", "NAN", "N/A"}
        has_no_stack = tech_stack_upper in {"NONE", "", "NULL", "NONE/CUSTOM", "NAN", "N/A"}

        # 1. Identify Core Business Scenario
        if has_no_website:
            scenario = "NO_DIGITAL_ASSET"
            category_title = "Zero Digital Asset — Complete Aggregator Dependency"
            pain_points = [
                f"{business_name} does not own a dedicated web platform, forcing 100% of online discovery through aggregators, social media, or Google Maps.",
                "Zero automated booking/inquiry funnel: potential clients looking up their brand cannot convert directly after hours.",
                "High customer acquisition costs: paying intermediary platform fees/commissions instead of building brand equity.",
                "Competitor vulnerability: nearby competitors with dedicated websites are ranking higher and capturing their high-ticket direct clients."
            ]
            package_name = "Turnkey Direct-Client Acquisition Platform"
            package_scope = [
                "Custom High-Converting Mobile-First Web Platform (Next.js / Webflow)",
                "Instant WhatsApp & One-Click Direct Booking Funnel",
                "Google Business Profile (GMB) Integration & Local SEO Synchronization",
                "Automated Review Collection & Trust Engine"
            ]
            pitch_angle = "Focus on asset ownership, bypassing commission cuts, and converting mobile map searchers into direct scheduled clients."
            closer_hook = f"We build an automated conversion hub for {business_name} so you never lose high-ticket clients to aggregators or local competitors."

        elif has_no_pixels and has_no_stack:
            scenario = "STATIC_UNTRACKED_SITE"
            category_title = "Dormant Web Presence — Active Traffic & Revenue Leak"
            pain_points = [
                f"{business_name} has an active web domain ({website}), but it lacks tracking tags, analytics, and dynamic conversion funnels.",
                "100% Retargeting Blindspot: Over 90% of visitors who leave without booking can never be remarketed or recaptured.",
                "No lead capture infrastructure: site acts as an online brochure rather than an active booking engine.",
                "Zero data attribution: the owner has no visibility into where their highest-paying clients originate."
            ]
            package_name = "Full-Funnel Conversion Engine & Retargeting Setup"
            package_scope = [
                "Meta Pixel & Google Tag Manager Installation with Custom Event Triggers",
                "Conversion-Focused Landing Page Redesign with Sticky CTA/WhatsApp Hooks",
                "Automated Visitor Recapture & Abandoned Lead Nurturing System",
                "Local Keyword Schema Markup to Boost Ranking in Organic Mobile Search"
            ]
            pitch_angle = "Explain that their current site is bleeding 90% of incoming visitors. Pitch installing conversion tracking and lead capture mechanics."
            closer_hook = f"You are already getting traffic to {website}, but without tracking and quick-booking hooks, you are paying to educate visitors who end up booking elsewhere."

        else:
            scenario = "OPTIMIZATION_READY"
            category_title = "Established Stack — Speed & Local SEO Domination"
            pain_points = [
                f"Existing web stack ({tech_stack_raw}) requires optimization to maintain search dominance against aggressive local competitors in {location}.",
                "Mobile page speed bottlenecks and non-optimized assets increase bounce rates during peak search hours.",
                "Local SEO gaps prevent the business from dominating the 'Near Me' top 3 map pack positions."
            ]
            package_name = "Local Market Domination & Speed Architecture"
            package_scope = [
                "Core Web Vitals & Mobile Speed Optimization",
                "Local Citation & Local SEO Map Pack Domination Package",
                "Retargeting Campaign Setup for Warm Web Visitors",
                "Conversion Funnel A/B Testing on Mobile Booking Forms"
            ]
            pitch_angle = "Pitch enterprise speed, outranking direct local competitors, and doubling mobile form completions."
            closer_hook = f"Your brand is established, but optimizing your mobile loading speed and local search pack will put you at #1 across {location}."

        # 2. Objection Handling Matrix
        objections = [
            {
                "objection": "We already get enough clients from word of mouth.",
                "rebuttal": f"Word of mouth is great, but when those referrals Google '{business_name}' to check hours or pricing, they need an instant direct booking interface. Without it, competitors sitting on top of search ads pick them off."
            },
            {
                "objection": "We already have a website / social media page.",
                "rebuttal": "Social pages keep users trapped in that social platform. An owned conversion platform captures direct phone numbers, enables retargeting, and builds your private customer database."
            },
            {
                "objection": "Send me a proposal on WhatsApp/Email.",
                "rebuttal": f"I can send that right over. To ensure I include the exact numbers for {location}, are you primarily looking to capture new patient/client inquiries or optimize your current follow-up flow?"
            }
        ]

        return {
            "scenario": scenario,
            "category_title": category_title,
            "pain_points": pain_points,
            "package_name": package_name,
            "package_scope": package_scope,
            "pitch_angle": pitch_angle,
            "closer_hook": closer_hook,
            "objections": objections,
            "network_scope": f"{network} ({entity_type})"
        }

    @classmethod
    def get_outreach_pitch(
        cls, pitch_type: str, business_name: str, category: str, location: str
    ) -> Tuple[str, str, str]:
        biz = business_name or "there"
        cat = category or "business"
        loc = location or "your area"

        if pitch_type in {"NO_ASSET", "NO_DIGITAL_ASSET"}:
            text = (
                f"Hello {biz} team, I was reviewing leading {cat}s in {loc} and noticed "
                f"you currently operate without an owned digital web platform. You are losing "
                f"high-intent direct searches to competitors and aggregators. We build automated, "
                f"high-converting web platforms that secure direct bookings. Open to a 2-minute overview?"
            )
            script = "Focus on asset ownership, zero commissions, and customer database control."
        elif pitch_type in {"STATIC_UNTRACKED_SITE", "CONVERSION_LEAK"}:
            text = (
                f"Hello {biz} team, during a technical audit of {cat}s in {loc}, we noticed "
                f"your website lacks active retargeting and conversion capture tags. You are paying for "
                f"or attracting traffic that leaves without converting. We deploy a turnkey tracking fix "
                f"that recaptures drop-offs. Would you like the audit breakdown?"
            )
            script = "Focus on paid/organic visitor recapture, tracking attribution, and ROI recovery."
        else:
            text = (
                f"Hello {biz} team, we conducted a market scan of {cat}s in {loc} and identified "
                f"specific mobile speed and search ranking bottlenecks holding your site back from dominating local search. "
                f"We help local market leaders capture the top 3 Google positions. Open to seeing the diagnostic?"
            )
            script = "Focus on local search domination, page speed benchmarks, and competitive displacement."

        encoded = urllib.parse.quote(text)
        return text, script, encoded


# Module exports
calculate_scores = SalesIntelligenceEngine.calculate_scores
generate_sales_brief = SalesIntelligenceEngine.generate_sales_brief
get_outreach_pitch = SalesIntelligenceEngine.get_outreach_pitch