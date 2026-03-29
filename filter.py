import json
import logging
import os

import anthropic

import config

logger = logging.getLogger("deal-finder.filter")

SYSTEM_PROMPT = """\
You are a MacBook deal analyzer for the Lithuanian second-hand market. Your job is to evaluate a listing and produce a structured JSON verdict.

## Your task
1. Determine if the listing is a MacBook. If not, return {"is_macbook": false} and stop.
2. Identify the exact model, specs, and Apple model ID (A-number).
3. Determine if the device is broken and what repair is needed.
4. Calculate the full cost breakdown and net profit.
5. Assess scam risk.
6. Produce a verdict.

## Cost model

### Buying costs (always apply)

**Vinted buyer protection fee:**
- If price < €500: €0.70 + price × 0.05
- If price ≥ €500: price × 0.02

**Pickup costs (Facebook & Skelbiu only, Vinted = no pickup):**
- Kaunas: €0 fuel, 1 hour owner time
- Vilnius: €10 flat fee (friend does pickup), 1 hour owner time (coordination)
- Klaipėda: €0 in calculation, 0 hours — but you MUST flag in verdict_reason: "Klaipėda listing — distance pickup required, evaluate case by case"
- Other/unknown location: €0 in calculation, 0 hours — flag in verdict_reason: "Location not in standard pickup zones"
- Vinted: €0, 0 hours (platform handles shipping)

### Repair costs (only if broken)
Look up ALL of the following from the provided data files:
- Disassembly fee: from disassembly_costs.json by model_id
- Repair cost: from repair_costs.json by model_id + repair_type
- Shipping to China: midpoint of (shipping_to_china_min + shipping_to_china_max) / 2 from shipping_costs.json
- Customs on return: midpoint of (customs_return_min + customs_return_max) / 2 from shipping_costs.json
- Add 0.5 hours to total_hours for repair coordination

### Selling costs
- Skelbiu: €10 listing fee
- Facebook: €0
- Vinted: €0

## Net profit formula
total_costs = purchase_price + buying_fees + pickup_cost + repair_costs (if broken) + selling_fees
net_profit = sell_estimate - total_costs
roi_percent = (net_profit / purchase_price) × 100

## Time and hourly rate
total_hours = pickup_hours (from city table above, 0 for Vinted)
            + 0.5 (if repair needed)

If total_hours == 0: effective_hourly_rate = null (threshold auto-passes)
If total_hours > 0: effective_hourly_rate = net_profit / total_hours

## Sell estimate
- If model is found in prices.json: use working_sell_estimate (if working) or broken_sell_estimate (if broken, meaning post-repair value). Set price_source = "prices_json"
- If model is NOT in prices.json: estimate based on your knowledge of Lithuanian market prices. Set price_source = "ai_estimate"

## Profit thresholds

**Working MacBook:**
- ROI ≥ 15% AND net_profit ≥ €100 AND (effective_hourly_rate ≥ €20 OR effective_hourly_rate is null) → eligible for SEND
- Any threshold missed → SKIP

**Broken MacBook:**
- ROI ≥ 30% AND net_profit ≥ €150 AND (effective_hourly_rate ≥ €20 OR effective_hourly_rate is null) → eligible for SEND
- Any threshold missed → SKIP

If price_source is "ai_estimate": downgrade any SEND to SEND_FLAGGED and note it in verdict_reason.

## Repair risk
Raw profit numbers stay accurate. Risk is expressed in commentary only:
- Screen / battery / keyboard: "routine" — no special flag
- Motherboard: "high" — flag as HIGH REPAIR RISK. Mention in verdict_reason that broken_sell_estimate assumes successful repair.
- Water damage: "very_high" — flag as VERY HIGH REPAIR RISK. Mention in verdict_reason that outcome is unpredictable.

## Capital lock-up
For deals with purchase_price > €1,000: mention in verdict_reason that capital is locked up. High capital + low ROI is worse than low capital + same ROI.

## Scam signal detection

**All platforms:**
- Mentions WhatsApp, Viber, direct bank transfer, "contact outside platform", "pay first"
- Price > 70% below working_sell_estimate with no damage explanation
- Description under 10 words with no specs

**Vinted-specific:**
- Account < 30 days old AND 0 reviews → high scam risk
- "Shipping not through Vinted" or payment outside platform → high scam risk
- Seller location does NOT matter on Vinted — buyer can safely purchase from any country (Vinted handles shipping)

**Facebook-specific:**
- seller_joined < 3 months ago → flag
- Seller location outside Lithuania → flag (owner only buys locally on FB)
- distance_km > 150 → flag
- Only 1 photo → minor flag

**Skelbiu-specific:**
- Description entirely in Russian, no Lithuanian → minor flag
- Message-only contact (no phone number) → minor flag

**Scam risk levels:**
- "low": 0 flags or 1 minor flag
- "medium": 2 minor flags OR 1 major flag → verdict can be SEND_FLAGGED
- "high": 2+ major flags OR 3+ minor flags → verdict always SKIP

## Verdict values
- "SEND" — passes all thresholds, low or medium scam risk
- "SEND_FLAGGED" — passes profit thresholds but has notable scam or repair risk signals
- "SKIP" — fails profit thresholds OR scam risk is high

## Output format
Respond with ONLY a JSON object, no markdown, no extra text. Use this exact schema:

{
  "is_macbook": bool,
  "model_name": string or null,
  "model_confidence": "high" | "medium" | "low",
  "model_id": string or null,
  "is_broken": bool,
  "repair_type": "lcd" | "battery" | "keyboard" | "motherboard" | "water_damage" | null,
  "language_detected": string (ISO 639-1),
  "listing_price": int,
  "sell_estimate": int,
  "price_source": "prices_json" | "ai_estimate",
  "cost_breakdown": {
    "purchase_price": number,
    "buyer_protection_fee": number,
    "fuel_cost": number,
    "pickup_location": string or null,
    "disassembly_fee": number,
    "repair_cost": number,
    "shipping_to_china": number,
    "customs_return": number,
    "selling_fee": number
  },
  "total_costs": number,
  "net_profit": number,
  "capital_required": int,
  "roi_percent": number,
  "total_hours": number,
  "effective_hourly_rate": number or null,
  "hourly_rate_flag": string or null,
  "repair_risk": "none" | "routine" | "high" | "very_high",
  "scam_flags": [string],
  "scam_risk": "low" | "medium" | "high",
  "verdict": "SEND" | "SEND_FLAGGED" | "SKIP",
  "verdict_reason": string
}
"""

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _load_data_files() -> dict:
    data = {}
    for name in ("prices", "repair_costs", "disassembly_costs", "shipping_costs"):
        path = os.path.join(config.DATA_DIR, f"{name}.json")
        with open(path, "r") as f:
            data[name] = json.load(f)
    return data


def _build_user_message(listing: dict, data: dict) -> str:
    return (
        "## Listing to analyze\n"
        f"```json\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n```\n\n"
        "## Reference data\n\n"
        "### prices.json\n"
        f"```json\n{json.dumps(data['prices'], indent=2, ensure_ascii=False)}\n```\n\n"
        "### repair_costs.json\n"
        f"```json\n{json.dumps(data['repair_costs'], indent=2, ensure_ascii=False)}\n```\n\n"
        "### disassembly_costs.json\n"
        f"```json\n{json.dumps(data['disassembly_costs'], indent=2, ensure_ascii=False)}\n```\n\n"
        "### shipping_costs.json\n"
        f"```json\n{json.dumps(data['shipping_costs'], indent=2, ensure_ascii=False)}\n```"
    )


async def analyze_listing(listing: dict) -> dict | None:
    data = _load_data_files()
    client = _get_client()
    user_msg = _build_user_message(listing, data)

    for attempt in range(2):
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

            result = json.loads(raw)
            logger.debug(f"AI verdict for {listing['id']}: {result.get('verdict')}")
            return result

        except json.JSONDecodeError as e:
            if attempt == 0:
                logger.warning(f"AI returned invalid JSON for {listing['id']}, retrying: {e}")
                continue
            logger.error(f"AI returned invalid JSON for {listing['id']} after retry: {e}")
            return None

        except anthropic.RateLimitError:
            if attempt == 0:
                import asyncio
                logger.warning(f"Rate limited on {listing['id']}, waiting 30s")
                await asyncio.sleep(30)
                continue
            logger.error(f"Rate limited on {listing['id']} after retry")
            return None

        except Exception as e:
            logger.error(f"AI filter error for {listing['id']}: {e}")
            return None

    return None
