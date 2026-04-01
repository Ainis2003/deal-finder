import asyncio
import json
import logging
import os

import anthropic

import config

logger = logging.getLogger("deal-finder.filter")

# ── Cached data (loaded once) ──────────────────────────────────────

_prices_data: dict | None = None
_repair_costs_data: dict | None = None
_shipping_data: dict | None = None
_model_reference_table: str | None = None
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _load_data():
    global _prices_data, _repair_costs_data, _shipping_data, _model_reference_table
    if _prices_data is not None:
        return

    with open(os.path.join(config.DATA_DIR, "prices.json")) as f:
        _prices_data = json.load(f)

    with open(os.path.join(config.DATA_DIR, "repair_costs.json")) as f:
        _repair_costs_data = json.load(f)

    with open(os.path.join(config.DATA_DIR, "shipping_costs.json")) as f:
        _shipping_data = json.load(f)

    _model_reference_table = _build_model_reference_table()


def _build_model_reference_table() -> str:
    lines = []
    for name, data in _prices_data.items():
        if name.startswith("_"):
            continue
        mid = data.get("model_id", "?")
        proc = data.get("processor", "?")
        base = data.get("base", "?")
        ram_options = list(data.get("ram", {}).keys())
        storage_options = list(data.get("storage", {}).keys())
        base_ram, base_storage = base.split("/") if "/" in base else ("?", "?")
        all_ram = [base_ram] + ram_options
        all_storage = [base_storage] + storage_options
        lines.append(f"{name} | {mid} | {proc} | RAM: {', '.join(all_ram)} | Storage: {', '.join(all_storage)}")
    return "\n".join(lines)


# ── System prompt ──────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """\
You identify MacBook models, assess condition, and detect scams in Lithuanian second-hand marketplace listings. Respond with ONLY a JSON object.

## Model identification
Read title + full description to determine: model name, year, processor, screen size, RAM, storage.
- Match by A-number (model_id) first if visible, then by name/specs/year clues.
- Determine screen size: 13" vs 14" vs 15" vs 16" from title, description, or model_id.
- If RAM/storage not stated anywhere, assume the base config for that model.
- If this is NOT a MacBook laptop (accessories, charger, case, iMac, Mac Mini, iPad, PC) → is_macbook: false.
- Confidence: "high" = processor + year + screen clear, "medium" = 2 of 3 clear, "low" = guessing.

Lithuanian hints: "nešiojamas kompiuteris" (laptop), "ekranas" (screen), "baterija" (battery), "klaviatūra" (keyboard), "būklė/būsena" (condition), "veikia" (works), "neveikia" (doesn't work), "sukilęs" (swollen), "įtrūkęs/sudaužytas" (cracked).

## Valid models
{model_table}

model_name in your output MUST exactly match one of the names above (left column). ram/storage MUST use the exact format shown (e.g. "8GB", "16GB", "512GB", "1TB", "2TB").

## Condition assessment
Determine if the MacBook is working or broken. If broken, list ALL repair types needed:
- "lcd" — cracked/broken/dead screen, display issues, backlight problems
- "battery" — dead/swollen/poor health battery, doesn't hold charge
- "keyboard" — broken/missing keys, non-functional keyboard
- "motherboard_water_damage" — dead board, liquid damage, no power, random shutdowns, logic board failure. This is ONE category (motherboard and water damage don't stack).

Repairs CAN stack: e.g. ["lcd", "battery"] if screen is cracked AND battery is dead.
For motherboard_water_damage: estimate repair cost in €100-300 range based on description severity (minor spill/corrosion = low end, dead board/no power = high end).

## Scam detection

**Auto-SKIP (set platform_skip_reason):**
- Vinted: seller has 0 reviews; description explicitly says ONLY in-person pickup (refuses shipping entirely) / only Orlen shipping / "message me first before buying" / "don't buy through Vinted". Note: mentioning hand-to-hand as an OPTION is fine — only skip if they refuse platform shipping. Seller location does NOT matter on Vinted (buyer protection + shipping works from anywhere).
- Skelbiu: seller location NOT near Kaunas or Vilnius (allowed: Kaunas, Vilnius, Jonava, Kėdainiai, Marijampolė, Alytus, Prienai, Kaišiadorys, Elektrėnai, Trakai, Ukmergė, Garliava, Domeikava, Lentvaris, Vievis, Grigiškės and surrounding)
- Facebook: clearly fraudulent listings only. Missing seller_joined is NOT auto-skip (Facebook often doesn't show it).

**Major scam signals:** WhatsApp/Viber/bank transfer mentions, price >70% below typical for model, <10 word description with no specs
**Minor scam signals:** Recent seller account (<2 years), Russian-only on Skelbiu, message-only contact, single photo (FB), placeholder price (€1), no description, Facebook seller_joined is null

**Risk:** "low" (0-1 minor), "medium" (2+ minor OR 1 major), "high" (2+ major OR 3+ minor)

## Output JSON (no markdown, no extra text)
{{"is_macbook":bool,"model_name":str|null,"model_id":str|null,"year":int|null,"processor":str|null,"screen_size":str|null,"ram":str|null,"storage":str|null,"model_confidence":"high"|"medium"|"low","is_broken":bool,"repairs_needed":[],"motherboard_water_damage_estimate":int|null,"condition_notes":str|null,"scam_flags":[],"scam_risk":"low"|"medium"|"high","platform_skip_reason":str|null,"language_detected":str|null,"verdict_reason":str}}

verdict_reason: 1-2 sentence summary of the listing quality and any concerns."""


# ── AI call ────────────────────────────────────────────────────────

async def _call_ai(listing: dict) -> dict | None:
    _load_data()
    client = _get_client()

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(model_table=_model_reference_table)
    clean = {k: v for k, v in listing.items() if k != "platform_raw"}
    user_msg = json.dumps(clean, indent=2, ensure_ascii=False)

    for attempt in range(2):
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

            result = json.loads(raw)
            logger.debug(f"AI identified {listing['id']}: {result.get('model_name')} conf={result.get('model_confidence')}")
            return result

        except json.JSONDecodeError as e:
            if attempt == 0:
                logger.warning(f"AI returned invalid JSON for {listing['id']}, retrying: {e}")
                continue
            logger.error(f"AI returned invalid JSON for {listing['id']} after retry: {e}")
            return None

        except anthropic.RateLimitError:
            if attempt == 0:
                logger.warning(f"Rate limited on {listing['id']}, waiting 30s")
                await asyncio.sleep(30)
                continue
            logger.error(f"Rate limited on {listing['id']} after retry")
            return None

        except Exception as e:
            logger.error(f"AI filter error for {listing['id']}: {e}")
            return None

    return None


# ── Sell price lookup ──────────────────────────────────────────────

def _lookup_sell_price(ai_result: dict) -> tuple[int | None, str]:
    _load_data()
    model_name = ai_result.get("model_name")
    model_id = ai_result.get("model_id")
    processor = ai_result.get("processor")
    ram = ai_result.get("ram")
    storage = ai_result.get("storage")

    # Try exact name match first
    model_data = _prices_data.get(model_name)

    # Fallback: match by model_id + processor
    if model_data is None and model_id and processor:
        for name, data in _prices_data.items():
            if name.startswith("_"):
                continue
            if data.get("model_id") == model_id and data.get("processor") == processor:
                model_data = data
                break

    # Fallback: match by model_id alone (if only one entry has that ID)
    if model_data is None and model_id:
        matches = [
            data for name, data in _prices_data.items()
            if not name.startswith("_") and data.get("model_id") == model_id
        ]
        if len(matches) == 1:
            model_data = matches[0]

    if model_data is None:
        return None, "ai_estimate"

    base_price = model_data.get("base_price", 0)
    if base_price == 0:
        return None, "ai_estimate"

    # Parse base config to know which RAM/storage have zero premium
    base_config = model_data.get("base", "")
    base_ram, base_storage = ("", "")
    if "/" in base_config:
        base_ram, base_storage = base_config.split("/", 1)

    # Calculate sell price
    price = base_price
    ram_premiums = model_data.get("ram", {})
    storage_premiums = model_data.get("storage", {})

    # RAM premium
    if ram and ram != base_ram:
        if ram in ram_premiums:
            price += ram_premiums[ram]
        else:
            return None, "ai_estimate"

    # Storage premium
    if storage and storage != base_storage:
        if storage in storage_premiums:
            price += storage_premiums[storage]
        else:
            return None, "ai_estimate"

    return price, "prices_json"


# ── Cost calculation ───────────────────────────────────────────────

def _calculate_costs(listing: dict, ai_result: dict, sell_price: int | None, price_source: str) -> dict:
    _load_data()
    platform = listing["platform"]
    purchase_price = listing["price"]

    # --- Buying fees ---
    if platform == "vinted":
        if purchase_price < 500:
            buyer_fee = 0.70 + purchase_price * 0.05
        else:
            buyer_fee = purchase_price * 0.02
        pickup_cost = 0
        pickup_hours = 0.0
        pickup_flag = None
    else:
        buyer_fee = 0
        city = (listing.get("seller_location") or "").lower().strip()
        # Try matching city against known pickup locations
        pickup_info = config.PICKUP_DEFAULT
        for city_key, info in config.PICKUP_COSTS.items():
            if city_key in city or city in city_key:
                pickup_info = info
                break
        pickup_cost = pickup_info.get("fuel", 0)
        pickup_hours = pickup_info.get("hours", 0.0)
        pickup_flag = pickup_info.get("flag")

    # --- Repair costs ---
    repair_total = 0.0
    repair_details = []
    repair_hours = 0.0
    repairs = ai_result.get("repairs_needed", [])

    if ai_result.get("is_broken") and repairs:
        repair_hours = config.REPAIR_COORDINATION_HOURS  # 0.5hr per device
        model_id = ai_result.get("model_id")
        repair_data = _repair_costs_data.get(model_id, {})

        shipping_mid = (_shipping_data["shipping_to_china_min"] + _shipping_data["shipping_to_china_max"]) / 2
        customs_mid = (_shipping_data["customs_return_min"] + _shipping_data["customs_return_max"]) / 2

        for repair_type in repairs:
            if isinstance(repair_data, dict) and not repair_data.get("_instructions"):
                type_data = repair_data.get(repair_type, {})
            else:
                type_data = {}

            # Repair cost
            if repair_type == "motherboard_water_damage":
                repair_cost = ai_result.get("motherboard_water_damage_estimate") or 200
            else:
                repair_cost = type_data.get("repair", 0)

            # Disassembly (stacks per repair type)
            disassembly = type_data.get("disassembly", 0)

            # Shipping + customs: ONLY for lcd and motherboard_water_damage
            if repair_type in ("lcd", "motherboard_water_damage"):
                shipping = shipping_mid
                customs = customs_mid
            else:
                shipping = 0
                customs = 0

            type_total = repair_cost + disassembly + shipping + customs
            repair_total += type_total
            repair_details.append({
                "type": repair_type,
                "repair": repair_cost,
                "disassembly": disassembly,
                "shipping": shipping,
                "customs": customs,
                "subtotal": type_total,
            })

    # --- Selling fee (always Skelbiu) ---
    selling_fee = config.SKELBIU_LISTING_FEE

    # --- Totals ---
    total_costs = purchase_price + buyer_fee + pickup_cost + repair_total + selling_fee
    net_profit = (sell_price - total_costs) if sell_price is not None else None
    roi_percent = (net_profit / purchase_price * 100) if (net_profit is not None and purchase_price > 0) else None

    total_hours = pickup_hours + repair_hours
    if total_hours > 0 and net_profit is not None:
        effective_hourly_rate = net_profit / total_hours
    else:
        effective_hourly_rate = None

    return {
        "purchase_price": purchase_price,
        "buyer_protection_fee": round(buyer_fee, 2),
        "pickup_cost": pickup_cost,
        "pickup_flag": pickup_flag,
        "repair_total": round(repair_total, 2),
        "repair_details": repair_details,
        "selling_fee": selling_fee,
        "total_costs": round(total_costs, 2),
        "sell_price": sell_price,
        "price_source": price_source,
        "net_profit": round(net_profit, 2) if net_profit is not None else None,
        "roi_percent": round(roi_percent, 1) if roi_percent is not None else None,
        "total_hours": total_hours,
        "effective_hourly_rate": round(effective_hourly_rate, 2) if effective_hourly_rate is not None else None,
    }


# ── Verdict logic ──────────────────────────────────────────────────

def _check_thresholds(is_broken: bool, profit: float, roi: float, hourly: float | None) -> bool:
    if is_broken:
        return roi >= 30 and profit >= 150 and (hourly is None or hourly >= 20)
    else:
        return roi >= 15 and profit >= 100 and (hourly is None or hourly >= 20)


def _calc_negotiate_target(listing: dict, ai_result: dict, costs: dict) -> dict | None:
    """Check if a 10% discount on listing price would pass thresholds. Returns negotiated costs or None."""
    negotiate_price = int(costs["purchase_price"] * 0.90)
    neg_costs = _calculate_costs(
        {**listing, "price": negotiate_price},
        ai_result,
        costs["sell_price"],
        costs["price_source"],
    )
    if neg_costs["net_profit"] is None:
        return None

    is_broken = ai_result.get("is_broken", False)
    if _check_thresholds(is_broken, neg_costs["net_profit"], neg_costs["roi_percent"], neg_costs["effective_hourly_rate"]):
        neg_costs["negotiate_price"] = negotiate_price
        return neg_costs
    return None


def _determine_verdict(listing: dict, ai_result: dict, costs: dict) -> tuple[str, str, dict | None]:
    """Returns (verdict, reason, negotiate_costs_or_None)."""
    # Not a MacBook
    if not ai_result.get("is_macbook"):
        return "SKIP", "Not a MacBook", None

    # Platform-specific auto-SKIP
    if ai_result.get("platform_skip_reason"):
        return "SKIP", ai_result["platform_skip_reason"], None

    # High scam risk
    if ai_result.get("scam_risk") == "high":
        return "SKIP", f"High scam risk: {', '.join(ai_result.get('scam_flags', []))}", None

    # No sell price
    if costs["sell_price"] is None or costs["net_profit"] is None:
        return "SKIP", "Could not determine sell price", None

    # Threshold checks
    is_broken = ai_result.get("is_broken", False)
    roi = costs["roi_percent"]
    profit = costs["net_profit"]
    hourly = costs["effective_hourly_rate"]

    passes = _check_thresholds(is_broken, profit, roi, hourly)

    if not passes:
        # Check if 10% negotiation would make it a deal
        neg = _calc_negotiate_target(listing, ai_result, costs)
        if neg is not None:
            reason = ai_result.get("verdict_reason", "")
            negotiate_price = neg["negotiate_price"]
            discount = costs["purchase_price"] - negotiate_price
            reason += f" | Negotiate to €{negotiate_price} (-€{discount}) → profit €{neg['net_profit']:.0f}, ROI {neg['roi_percent']:.0f}%"
            return "SEND_NEGOTIATE", reason, neg

        # Not even negotiable
        reasons = []
        if is_broken:
            if roi is not None and roi < 30:
                reasons.append(f"ROI {roi:.0f}% < 30%")
            if profit < 150:
                reasons.append(f"Profit €{profit:.0f} < €150")
        else:
            if roi is not None and roi < 15:
                reasons.append(f"ROI {roi:.0f}% < 15%")
            if profit < 100:
                reasons.append(f"Profit €{profit:.0f} < €100")
        if hourly is not None and hourly < 20:
            reasons.append(f"€{hourly:.0f}/hr < €20")
        return "SKIP", f"Below thresholds: {'; '.join(reasons)}", None

    # Passes thresholds — determine SEND vs SEND_FLAGGED
    verdict = "SEND"
    flags = []

    if costs["price_source"] == "ai_estimate":
        verdict = "SEND_FLAGGED"
        flags.append("Sell price is AI estimate (model not in database)")

    if ai_result.get("scam_risk") == "medium":
        verdict = "SEND_FLAGGED"
        flags.append(f"Medium scam risk")

    repairs = ai_result.get("repairs_needed", [])
    if "motherboard_water_damage" in repairs:
        verdict = "SEND_FLAGGED"
        flags.append("HIGH REPAIR RISK: motherboard/water damage")

    if ai_result.get("model_confidence") == "medium":
        verdict = "SEND_FLAGGED"
        flags.append("Medium confidence on model identification")

    if costs["pickup_flag"]:
        verdict = "SEND_FLAGGED"
        flags.append(costs["pickup_flag"])

    if costs["purchase_price"] > 1000 and roi is not None and roi < 20:
        flags.append(f"Capital lock-up: €{costs['purchase_price']} at {roi:.0f}% ROI")

    # Build verdict reason
    reason = ai_result.get("verdict_reason", "")
    if flags:
        reason += " | " + "; ".join(flags)

    return verdict, reason, None


# ── Main entry point ───────────────────────────────────────────────

async def analyze_listing(listing: dict) -> dict | None:
    # Step 1: AI identification + scam detection
    ai_result = await _call_ai(listing)
    if ai_result is None:
        return None

    # Quick exit for non-MacBooks
    if not ai_result.get("is_macbook"):
        return {
            "verdict": "SKIP",
            "verdict_reason": ai_result.get("verdict_reason") or "Not a MacBook",
            "model_name": None,
            "net_profit": None,
        }

    # Quick exit for platform SKIPs (keep AI identification data)
    if ai_result.get("platform_skip_reason"):
        return {
            "verdict": "SKIP",
            "verdict_reason": ai_result["platform_skip_reason"],
            "model_name": ai_result.get("model_name"),
            "model_id": ai_result.get("model_id"),
            "net_profit": None,
        }

    # Step 2: Price lookup (Python)
    sell_price, price_source = _lookup_sell_price(ai_result)

    # Step 3: Cost calculation (Python)
    costs = _calculate_costs(listing, ai_result, sell_price, price_source)

    # Step 4: Verdict (Python)
    verdict, verdict_reason, negotiate_costs = _determine_verdict(listing, ai_result, costs)

    # Step 5: Assemble result (compatible with bot.py)
    return {
        "model_name": ai_result.get("model_name"),
        "model_confidence": ai_result.get("model_confidence"),
        "model_id": ai_result.get("model_id"),
        "year": ai_result.get("year"),
        "processor": ai_result.get("processor"),
        "ram": ai_result.get("ram"),
        "storage": ai_result.get("storage"),
        "screen_size": ai_result.get("screen_size"),
        "is_broken": ai_result.get("is_broken", False),
        "repairs_needed": ai_result.get("repairs_needed", []),
        "condition_notes": ai_result.get("condition_notes"),
        "sell_price": costs["sell_price"],
        "price_source": costs["price_source"],
        "cost_breakdown": {
            "purchase_price": costs["purchase_price"],
            "buyer_protection_fee": costs["buyer_protection_fee"],
            "pickup_cost": costs["pickup_cost"],
            "repair_total": costs["repair_total"],
            "selling_fee": costs["selling_fee"],
        },
        "total_costs": costs["total_costs"],
        "net_profit": costs["net_profit"],
        "roi_percent": costs["roi_percent"],
        "total_hours": costs["total_hours"],
        "effective_hourly_rate": costs["effective_hourly_rate"],
        "scam_flags": ai_result.get("scam_flags", []),
        "scam_risk": ai_result.get("scam_risk", "low"),
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "negotiate": {
            "target_price": negotiate_costs["negotiate_price"],
            "profit_if_negotiated": negotiate_costs["net_profit"],
            "roi_if_negotiated": negotiate_costs["roi_percent"],
        } if negotiate_costs else None,
        "language_detected": ai_result.get("language_detected"),
    }
