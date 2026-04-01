#!/usr/bin/env python3
"""Comprehensive test suite for deal-finder system."""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test")

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


# ════════════════════════════════════════════════════════════════════
# TEST 1: Config validation
# ════════════════════════════════════════════════════════════════════
def test_config():
    print("\n🔧 TEST 1: Config validation")
    import config

    check("ANTHROPIC_API_KEY set", config.ANTHROPIC_API_KEY and len(config.ANTHROPIC_API_KEY) > 10)
    check("TELEGRAM_BOT_TOKEN set", config.TELEGRAM_BOT_TOKEN and len(config.TELEGRAM_BOT_TOKEN) > 10)
    check("TELEGRAM_CHAT_ID set", config.TELEGRAM_CHAT_ID and len(config.TELEGRAM_CHAT_ID) > 5)
    check("DATA_DIR exists", os.path.isdir(config.DATA_DIR))
    check("LOG_DIR exists", os.path.isdir(config.LOG_DIR))
    check("PICKUP_COSTS has kaunas", "kaunas" in config.PICKUP_COSTS)
    check("PICKUP_COSTS has vilnius", "vilnius" in config.PICKUP_COSTS)
    check("SKELBIU_LISTING_FEE is 10", config.SKELBIU_LISTING_FEE == 10)
    check("REPAIR_COORDINATION_HOURS is 0.5", config.REPAIR_COORDINATION_HOURS == 0.5)


# ════════════════════════════════════════════════════════════════════
# TEST 2: Data files integrity
# ════════════════════════════════════════════════════════════════════
def test_data_files():
    print("\n📁 TEST 2: Data files integrity")

    # prices.json
    with open("data/prices.json") as f:
        prices = json.load(f)
    models = {k: v for k, v in prices.items() if not k.startswith("_")}
    check("prices.json loads", len(models) > 0, f"got {len(models)} models")
    check("prices.json has 25+ models", len(models) >= 25, f"got {len(models)}")

    for name, data in models.items():
        has_fields = all(k in data for k in ("model_id", "processor", "base", "base_price"))
        if not has_fields:
            check(f"prices.json {name} has required fields", False, f"missing fields in {name}")
            break
    else:
        check("All models have required fields", True)

    # Check base config parseable
    bad_bases = []
    for name, data in models.items():
        base = data.get("base", "")
        if "/" not in base:
            bad_bases.append(name)
    check("All base configs have RAM/Storage format", len(bad_bases) == 0, f"bad: {bad_bases}")

    # repair_costs.json
    with open("data/repair_costs.json") as f:
        repairs = json.load(f)
    repair_models = {k: v for k, v in repairs.items() if not k.startswith("_")}
    check("repair_costs.json loads", len(repair_models) > 0)

    # Check all price model_ids have repair entries
    price_ids = {v["model_id"] for v in models.values()}
    repair_ids = set(repair_models.keys())
    missing = price_ids - repair_ids
    check("All price model_ids have repair costs", len(missing) == 0, f"missing: {missing}")

    # Check repair structure
    for mid, data in repair_models.items():
        has_types = all(k in data for k in ("lcd", "battery", "keyboard", "motherboard_water_damage"))
        if not has_types:
            check(f"repair {mid} has all types", False, f"missing types in {mid}")
            break
    else:
        check("All repair entries have lcd/battery/keyboard/motherboard_water_damage", True)

    # Check lcd prices are non-zero
    zero_lcd = [mid for mid, d in repair_models.items() if d.get("lcd", {}).get("repair", 0) == 0]
    check("All LCD repair prices non-zero", len(zero_lcd) == 0, f"zero: {zero_lcd}")

    # shipping_costs.json
    with open("data/shipping_costs.json") as f:
        shipping = json.load(f)
    check("shipping_costs.json has all fields",
          all(k in shipping for k in ("shipping_to_china_min", "shipping_to_china_max", "customs_return_min", "customs_return_max")))


# ════════════════════════════════════════════════════════════════════
# TEST 3: Database operations
# ════════════════════════════════════════════════════════════════════
async def test_db():
    print("\n🗄️ TEST 3: Database operations")
    import db

    await db.init_db()
    check("DB init succeeds", True)

    # Mark seen with full details
    await db.mark_seen("test_db_001", "vinted", url="https://test.com/1", title="Test MB", price=500)
    seen = await db.is_seen("test_db_001")
    check("mark_seen + is_seen works", seen)

    # Duplicate insert should not fail
    await db.mark_seen("test_db_001", "vinted", url="https://test.com/1", title="Test MB", price=500)
    check("Duplicate insert doesn't crash", True)

    # Update verdict
    await db.update_verdict("test_db_001", "SEND")
    import aiosqlite, config
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cursor = await conn.execute("SELECT verdict FROM seen_listings WHERE id = ?", ("test_db_001",))
        row = await cursor.fetchone()
    check("update_verdict works", row and row[0] == "SEND", f"got {row}")

    # Mark more for count tests
    await db.mark_seen("test_db_002", "facebook", url="https://fb.com/2", title="Test FB", price=300)
    await db.mark_seen("test_db_003", "skelbiu", url="https://skelbiu.lt/3", title="Test SK", price=400)

    # Today counts
    today_counts = await db.get_today_counts()
    check("get_today_counts returns dict", isinstance(today_counts, dict))
    check("get_today_counts has vinted", today_counts.get("vinted", 0) >= 1, f"got {today_counts}")

    # Total counts
    total_counts = await db.get_total_counts()
    check("get_total_counts returns dict", isinstance(total_counts, dict))

    # Last seen time
    last = await db.get_last_seen_time()
    check("get_last_seen_time returns datetime", last is not None, f"got {last}")

    # Clean up test entries
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute("DELETE FROM seen_listings WHERE id LIKE 'test_db_%'")
        await conn.commit()
    check("Cleanup test entries", True)


# ════════════════════════════════════════════════════════════════════
# TEST 4: Price lookup
# ════════════════════════════════════════════════════════════════════
def test_price_lookup():
    print("\n💰 TEST 4: Price lookup")
    from filter import _load_data, _lookup_sell_price
    _load_data()

    # Exact name match, base config
    price, src = _lookup_sell_price({"model_name": "MacBook Air M1 13-inch 2020", "model_id": "A2337", "processor": "M1", "ram": "8GB", "storage": "256GB"})
    check("M1 Air base config lookup", price == 300 and src == "prices_json", f"got {price}/{src}")

    # Name match with RAM/storage premium
    price, src = _lookup_sell_price({"model_name": "MacBook Air M1 13-inch 2020", "model_id": "A2337", "processor": "M1", "ram": "16GB", "storage": "1TB"})
    expected = 300 + 60 + 50  # base + ram + storage
    check("M1 Air 16GB/1TB premium calc", price == expected and src == "prices_json", f"got {price} expected {expected}")

    # Fallback by model_id + processor (no name match)
    price, src = _lookup_sell_price({"model_name": "Wrong Name", "model_id": "A2337", "processor": "M1", "ram": "8GB", "storage": "256GB"})
    check("Fallback by model_id+processor", price == 300 and src == "prices_json", f"got {price}/{src}")

    # Fallback by model_id alone (unique)
    price, src = _lookup_sell_price({"model_name": "Wrong", "model_id": "A2681", "processor": "Wrong", "ram": "8GB", "storage": "256GB"})
    check("Fallback by model_id alone (unique)", price is not None and src == "prices_json", f"got {price}/{src}")

    # Shared model_id (A2442 = M1 Pro AND M1 Max) — needs processor to disambiguate
    price_pro, _ = _lookup_sell_price({"model_name": "X", "model_id": "A2442", "processor": "M1 Pro", "ram": "16GB", "storage": "512GB"})
    price_max, _ = _lookup_sell_price({"model_name": "X", "model_id": "A2442", "processor": "M1 Max", "ram": "32GB", "storage": "512GB"})
    check("Shared model_id disambiguated by processor", price_pro != price_max and price_pro is not None and price_max is not None,
          f"Pro={price_pro}, Max={price_max}")

    # Unknown model → ai_estimate
    price, src = _lookup_sell_price({"model_name": "MacBook Pro M99 2030", "model_id": "A9999", "processor": "M99", "ram": "8GB", "storage": "256GB"})
    check("Unknown model → ai_estimate", price is None and src == "ai_estimate", f"got {price}/{src}")

    # Unknown RAM config → ai_estimate
    price, src = _lookup_sell_price({"model_name": "MacBook Air M1 13-inch 2020", "model_id": "A2337", "processor": "M1", "ram": "64GB", "storage": "256GB"})
    check("Unknown RAM → ai_estimate", price is None and src == "ai_estimate", f"got {price}/{src}")

    # Unknown storage config → ai_estimate
    price, src = _lookup_sell_price({"model_name": "MacBook Air M1 13-inch 2020", "model_id": "A2337", "processor": "M1", "ram": "8GB", "storage": "4TB"})
    check("Unknown storage → ai_estimate", price is None and src == "ai_estimate", f"got {price}/{src}")

    # None RAM/storage → base config (both match base, no premium added)
    price, src = _lookup_sell_price({"model_name": "MacBook Air M1 13-inch 2020", "model_id": "A2337", "processor": "M1", "ram": None, "storage": None})
    check("None RAM/storage → base price", price == 300 and src == "prices_json", f"got {price}/{src}")


# ════════════════════════════════════════════════════════════════════
# TEST 5: Cost calculation
# ════════════════════════════════════════════════════════════════════
def test_cost_calculation():
    print("\n🧮 TEST 5: Cost calculation")
    from filter import _load_data, _calculate_costs
    _load_data()

    # Vinted working MacBook < €500
    listing = {"platform": "vinted", "price": 350, "seller_location": "Kaunas"}
    ai = {"is_broken": False, "repairs_needed": [], "model_id": "A2337"}
    costs = _calculate_costs(listing, ai, 400, "prices_json")
    expected_fee = 0.70 + 350 * 0.05  # = 18.20
    check("Vinted fee < €500", abs(costs["buyer_protection_fee"] - expected_fee) < 0.01, f"got {costs['buyer_protection_fee']} expected {expected_fee}")
    check("Vinted no pickup cost", costs["pickup_cost"] == 0)
    check("Vinted selling fee €10", costs["selling_fee"] == 10)
    check("Vinted working: 0 hours", costs["total_hours"] == 0.0)
    check("Vinted working: hourly is None", costs["effective_hourly_rate"] is None)
    expected_total = 350 + expected_fee + 0 + 0 + 10
    check("Vinted total costs", abs(costs["total_costs"] - expected_total) < 0.01, f"got {costs['total_costs']} expected {expected_total}")

    # Vinted fee >= €500
    listing2 = {"platform": "vinted", "price": 600, "seller_location": "Kaunas"}
    costs2 = _calculate_costs(listing2, ai, 800, "prices_json")
    expected_fee2 = 600 * 0.02  # = 12.00
    check("Vinted fee ≥ €500", abs(costs2["buyer_protection_fee"] - expected_fee2) < 0.01, f"got {costs2['buyer_protection_fee']} expected {expected_fee2}")

    # Vinted fee boundary: exactly €500
    listing_500 = {"platform": "vinted", "price": 500, "seller_location": "Kaunas"}
    costs_500 = _calculate_costs(listing_500, ai, 700, "prices_json")
    expected_500 = 500 * 0.02  # = 10.00 (>= 500)
    check("Vinted fee at exactly €500", abs(costs_500["buyer_protection_fee"] - expected_500) < 0.01, f"got {costs_500['buyer_protection_fee']}")

    # Vinted fee boundary: €499
    listing_499 = {"platform": "vinted", "price": 499, "seller_location": "Kaunas"}
    costs_499 = _calculate_costs(listing_499, ai, 700, "prices_json")
    expected_499 = 0.70 + 499 * 0.05  # = 25.65
    check("Vinted fee at €499", abs(costs_499["buyer_protection_fee"] - expected_499) < 0.01, f"got {costs_499['buyer_protection_fee']}")

    # Facebook Kaunas pickup
    listing3 = {"platform": "facebook", "price": 400, "seller_location": "Kaunas"}
    costs3 = _calculate_costs(listing3, ai, 600, "prices_json")
    check("FB Kaunas: €0 fuel", costs3["pickup_cost"] == 0)
    check("FB Kaunas: 1hr pickup", costs3["total_hours"] == 1.0)
    check("FB: no buyer fee", costs3["buyer_protection_fee"] == 0)

    # Facebook Vilnius pickup
    listing4 = {"platform": "facebook", "price": 400, "seller_location": "Vilnius"}
    costs4 = _calculate_costs(listing4, ai, 600, "prices_json")
    check("FB Vilnius: €10 fuel", costs4["pickup_cost"] == 10)

    # Skelbiu unknown city → pickup_flag
    listing5 = {"platform": "skelbiu", "price": 400, "seller_location": "Šiauliai"}
    costs5 = _calculate_costs(listing5, ai, 600, "prices_json")
    check("Skelbiu unknown city: pickup_flag set", costs5["pickup_flag"] is not None, f"flag: {costs5['pickup_flag']}")

    # Broken MacBook: single repair (LCD)
    ai_broken = {"is_broken": True, "repairs_needed": ["lcd"], "model_id": "A2337", "motherboard_water_damage_estimate": None}
    costs_lcd = _calculate_costs({"platform": "vinted", "price": 200, "seller_location": ""}, ai_broken, 300, "prices_json")
    check("LCD repair: repair_total > 0", costs_lcd["repair_total"] > 0, f"got {costs_lcd['repair_total']}")
    check("LCD repair: includes shipping", any(d["shipping"] > 0 for d in costs_lcd["repair_details"]))
    check("LCD repair: 0.5hr coordination", costs_lcd["total_hours"] == 0.5)

    # Broken: stacked repairs (LCD + battery)
    ai_stack = {"is_broken": True, "repairs_needed": ["lcd", "battery"], "model_id": "A2337", "motherboard_water_damage_estimate": None}
    costs_stack = _calculate_costs({"platform": "vinted", "price": 150, "seller_location": ""}, ai_stack, 300, "prices_json")
    check("Stacked repairs: 2 entries in repair_details", len(costs_stack["repair_details"]) == 2, f"got {len(costs_stack['repair_details'])}")
    lcd_detail = next(d for d in costs_stack["repair_details"] if d["type"] == "lcd")
    bat_detail = next(d for d in costs_stack["repair_details"] if d["type"] == "battery")
    check("LCD has shipping", lcd_detail["shipping"] > 0)
    check("Battery NO shipping", bat_detail["shipping"] == 0)
    check("Both have disassembly", lcd_detail["disassembly"] > 0 and bat_detail["disassembly"] > 0)
    check("Stacked: still 0.5hr (per device)", costs_stack["total_hours"] == 0.5)

    # Broken: motherboard_water_damage with AI estimate
    ai_mb = {"is_broken": True, "repairs_needed": ["motherboard_water_damage"], "model_id": "A2337", "motherboard_water_damage_estimate": 250}
    costs_mb = _calculate_costs({"platform": "vinted", "price": 100, "seller_location": ""}, ai_mb, 300, "prices_json")
    mb_detail = costs_mb["repair_details"][0]
    check("Motherboard uses AI estimate (€250)", mb_detail["repair"] == 250, f"got {mb_detail['repair']}")
    check("Motherboard has shipping", mb_detail["shipping"] > 0)

    # Motherboard with no estimate → fallback to 200
    ai_mb_none = {"is_broken": True, "repairs_needed": ["motherboard_water_damage"], "model_id": "A2337", "motherboard_water_damage_estimate": None}
    costs_mb_none = _calculate_costs({"platform": "vinted", "price": 100, "seller_location": ""}, ai_mb_none, 300, "prices_json")
    check("Motherboard no estimate → fallback €200", costs_mb_none["repair_details"][0]["repair"] == 200)

    # Keyboard: no shipping, local repair
    ai_kb = {"is_broken": True, "repairs_needed": ["keyboard"], "model_id": "A2337", "motherboard_water_damage_estimate": None}
    costs_kb = _calculate_costs({"platform": "vinted", "price": 100, "seller_location": ""}, ai_kb, 300, "prices_json")
    kb_detail = costs_kb["repair_details"][0]
    check("Keyboard: no shipping", kb_detail["shipping"] == 0)
    check("Keyboard: no customs", kb_detail["customs"] == 0)
    check("Keyboard: repair = €2.75", kb_detail["repair"] == 2.75)

    # Profit calculation
    check("Net profit correct", costs_lcd["net_profit"] == costs_lcd["sell_price"] - costs_lcd["total_costs"])

    # ROI calculation
    if costs_lcd["net_profit"] is not None and costs_lcd["purchase_price"] > 0:
        expected_roi = round(costs_lcd["net_profit"] / costs_lcd["purchase_price"] * 100, 1)
        check("ROI correct", costs_lcd["roi_percent"] == expected_roi, f"got {costs_lcd['roi_percent']} expected {expected_roi}")

    # Sell price None → profit None
    costs_none = _calculate_costs({"platform": "vinted", "price": 200, "seller_location": ""}, ai, None, "ai_estimate")
    check("Sell price None → net_profit None", costs_none["net_profit"] is None)
    check("Sell price None → roi None", costs_none["roi_percent"] is None)


# ════════════════════════════════════════════════════════════════════
# TEST 6: Verdict logic
# ════════════════════════════════════════════════════════════════════
def test_verdict():
    print("\n⚖️ TEST 6: Verdict logic")
    from filter import _determine_verdict

    listing = {"platform": "vinted", "price": 200}

    # Not a MacBook
    v, r = _determine_verdict(listing, {"is_macbook": False}, {})
    check("Not MacBook → SKIP", v == "SKIP")

    # Platform skip
    v, r = _determine_verdict(listing, {"is_macbook": True, "platform_skip_reason": "0 reviews"}, {})
    check("Platform skip → SKIP", v == "SKIP" and "0 reviews" in r)

    # High scam
    v, r = _determine_verdict(listing, {"is_macbook": True, "platform_skip_reason": None, "scam_risk": "high", "scam_flags": ["WhatsApp", "bank transfer"]}, {"sell_price": 400, "net_profit": 100, "roi_percent": 50, "effective_hourly_rate": None, "purchase_price": 200, "price_source": "prices_json", "pickup_flag": None})
    check("High scam → SKIP", v == "SKIP")

    # No sell price
    v, r = _determine_verdict(listing, {"is_macbook": True, "platform_skip_reason": None, "scam_risk": "low", "scam_flags": []}, {"sell_price": None, "net_profit": None, "roi_percent": None, "effective_hourly_rate": None, "purchase_price": 200, "price_source": "ai_estimate", "pickup_flag": None})
    check("No sell price → SKIP", v == "SKIP")

    # Working passes thresholds → SEND
    good = {"is_macbook": True, "platform_skip_reason": None, "scam_risk": "low", "scam_flags": [], "is_broken": False, "repairs_needed": [], "model_confidence": "high", "verdict_reason": "Good deal."}
    costs_good = {"sell_price": 400, "net_profit": 150, "roi_percent": 75, "effective_hourly_rate": None, "purchase_price": 200, "price_source": "prices_json", "pickup_flag": None}
    v, r = _determine_verdict(listing, good, costs_good)
    check("Working good deal → SEND", v == "SEND", f"got {v}: {r}")

    # Working below ROI threshold
    costs_low_roi = {**costs_good, "roi_percent": 10, "net_profit": 120}
    v, r = _determine_verdict(listing, good, costs_low_roi)
    check("Working ROI 10% < 15% → SKIP", v == "SKIP")

    # Working below profit threshold
    costs_low_profit = {**costs_good, "net_profit": 50, "roi_percent": 25}
    v, r = _determine_verdict(listing, good, costs_low_profit)
    check("Working profit €50 < €100 → SKIP", v == "SKIP")

    # Working below hourly threshold
    costs_low_hourly = {**costs_good, "effective_hourly_rate": 15}
    v, r = _determine_verdict({"platform": "facebook", "price": 200}, good, costs_low_hourly)
    check("Working €15/hr < €20 → SKIP", v == "SKIP")

    # Broken passes thresholds → SEND
    broken_good = {**good, "is_broken": True, "repairs_needed": ["lcd"]}
    costs_broken = {"sell_price": 400, "net_profit": 200, "roi_percent": 100, "effective_hourly_rate": 400, "purchase_price": 200, "price_source": "prices_json", "pickup_flag": None}
    v, r = _determine_verdict(listing, broken_good, costs_broken)
    check("Broken good deal → SEND", v == "SEND", f"got {v}: {r}")

    # Broken below 30% ROI
    costs_broken_low = {**costs_broken, "roi_percent": 20, "net_profit": 160}
    v, r = _determine_verdict(listing, broken_good, costs_broken_low)
    check("Broken ROI 20% < 30% → SKIP", v == "SKIP")

    # Broken below €150 profit
    costs_broken_low2 = {**costs_broken, "net_profit": 100, "roi_percent": 50}
    v, r = _determine_verdict(listing, broken_good, costs_broken_low2)
    check("Broken profit €100 < €150 → SKIP", v == "SKIP")

    # AI estimate → SEND_FLAGGED
    costs_ai = {**costs_good, "price_source": "ai_estimate"}
    v, r = _determine_verdict(listing, good, costs_ai)
    check("AI estimate → SEND_FLAGGED", v == "SEND_FLAGGED")

    # Medium scam → SEND_FLAGGED
    med_scam = {**good, "scam_risk": "medium", "scam_flags": ["new account", "no description"]}
    v, r = _determine_verdict(listing, med_scam, costs_good)
    check("Medium scam → SEND_FLAGGED", v == "SEND_FLAGGED")

    # Motherboard repair → SEND_FLAGGED
    mb_repair = {**good, "is_broken": True, "repairs_needed": ["motherboard_water_damage"]}
    v, r = _determine_verdict(listing, mb_repair, costs_broken)
    check("Motherboard repair → SEND_FLAGGED", v == "SEND_FLAGGED")
    check("Motherboard flagged in reason", "REPAIR RISK" in r, f"reason: {r}")

    # Medium confidence → SEND_FLAGGED
    med_conf = {**good, "model_confidence": "medium"}
    v, r = _determine_verdict(listing, med_conf, costs_good)
    check("Medium confidence → SEND_FLAGGED", v == "SEND_FLAGGED")

    # Capital lock-up flag
    costs_capital = {**costs_good, "purchase_price": 1500, "roi_percent": 18, "net_profit": 270}
    v, r = _determine_verdict({"platform": "vinted", "price": 1500}, good, costs_capital)
    check("Capital lock-up flagged", "Capital lock-up" in r, f"reason: {r}")

    # Vinted 0 hours → hourly None → passes hourly check
    costs_0h = {**costs_good, "total_hours": 0, "effective_hourly_rate": None}
    v, r = _determine_verdict(listing, good, costs_0h)
    check("Vinted 0 hours: hourly None passes", v == "SEND", f"got {v}")


# ════════════════════════════════════════════════════════════════════
# TEST 7: Bot formatting
# ════════════════════════════════════════════════════════════════════
def test_bot_formatting():
    print("\n📱 TEST 7: Bot message formatting")
    from bot import format_deal_message, record_stat, daily_stats

    listing = {
        "id": "vinted_test",
        "platform": "vinted",
        "title": "MacBook Air M1",
        "url": "https://vinted.lt/items/test",
        "price": 250,
        "seller_reviews": 47,
        "seller_negative_reviews": 2,
        "seller_joined": "2021-03",
        "seller_location": "Kaunas",
    }

    verdict_send = {
        "verdict": "SEND",
        "model_name": "MacBook Air M1 13-inch 2020",
        "model_id": "A2337",
        "year": 2020,
        "processor": "M1",
        "ram": "8GB",
        "storage": "256GB",
        "screen_size": "13.3",
        "is_broken": False,
        "repairs_needed": [],
        "condition_notes": "Clean, 92% battery",
        "sell_price": 300,
        "price_source": "prices_json",
        "cost_breakdown": {"purchase_price": 250, "buyer_protection_fee": 13.20, "pickup_cost": 0, "repair_total": 0, "selling_fee": 10},
        "total_costs": 273.20,
        "net_profit": 26.80,
        "roi_percent": 10.7,
        "total_hours": 0,
        "effective_hourly_rate": None,
        "scam_flags": [],
        "scam_risk": "low",
        "verdict_reason": "Clean M1 Air at good price.",
    }

    msg = format_deal_message(verdict_send, listing)
    check("SEND message has green header", "🟢" in msg)
    check("Has model name", "MacBook Air M1 13-inch 2020" in msg)
    check("Has model_id", "A2337" in msg)
    check("Has specs", "M1" in msg and "8GB" in msg and "256GB" in msg)
    check("Has price", "€250" in msg)
    check("Has sell price", "€300" in msg)
    check("Has scam risk", "LOW" in msg)
    check("Has seller reviews", "47 reviews" in msg)
    check("Has negative reviews", "2 negative" in msg)
    check("Has URL", "https://vinted.lt" in msg)

    # SEND_FLAGGED
    verdict_flagged = {**verdict_send, "verdict": "SEND_FLAGGED"}
    msg2 = format_deal_message(verdict_flagged, listing)
    check("FLAGGED message has yellow header", "🟡" in msg2)

    # Broken with stacked repairs
    verdict_broken = {
        **verdict_send,
        "is_broken": True,
        "repairs_needed": ["lcd", "battery"],
        "condition_notes": None,
    }
    msg3 = format_deal_message(verdict_broken, listing)
    check("Broken shows repair types", "lcd" in msg3 and "battery" in msg3)
    check("Has wrench emoji", "🔧" in msg3)

    # Motherboard water damage warning
    verdict_mb = {**verdict_send, "is_broken": True, "repairs_needed": ["motherboard_water_damage"]}
    msg4 = format_deal_message(verdict_mb, listing)
    check("Motherboard shows warning", "⚠️" in msg4)

    # Test record_stat
    record_stat("vinted", "SEND")
    record_stat("vinted", "SKIP")
    record_stat("facebook", "SEND_FLAGGED")
    record_stat("skelbiu", "ERROR")
    check("Stats tracked", daily_stats["vinted"]["sent"] >= 1 and daily_stats["vinted"]["skipped"] >= 1)
    check("Stats cross-platform", daily_stats["facebook"]["flagged"] >= 1)
    check("Error stats", daily_stats["skelbiu"]["errors"] >= 1)


# ════════════════════════════════════════════════════════════════════
# TEST 8: Listing logger
# ════════════════════════════════════════════════════════════════════
def test_listing_logger():
    print("\n📝 TEST 8: Listing logger (JSONL)")
    from orchestrator import _log_listing
    from datetime import datetime

    listing = {"id": "test_log_001", "platform": "vinted", "url": "https://test.com", "title": "Test MB", "price": 300}
    verdict = {"verdict": "SKIP", "model_name": "MacBook Air M1", "net_profit": -50}

    _log_listing(listing, verdict)

    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join("logs", f"listings_{today}.jsonl")
    check("JSONL file created", os.path.exists(log_path))

    with open(log_path) as f:
        lines = f.readlines()
    last_line = json.loads(lines[-1])
    check("JSONL has correct id", last_line["id"] == "test_log_001")
    check("JSONL has verdict", last_line["verdict"] == "SKIP")
    check("JSONL has model", last_line["model"] == "MacBook Air M1")
    check("JSONL has timestamp", "ts" in last_line)

    # Test with None verdict (error case)
    _log_listing(listing, None)
    with open(log_path) as f:
        lines = f.readlines()
    last_line = json.loads(lines[-1])
    check("JSONL handles None verdict", last_line["verdict"] == "ERROR")


# ════════════════════════════════════════════════════════════════════
# TEST 9: Pre-filter
# ════════════════════════════════════════════════════════════════════
def test_pre_filter():
    print("\n🔍 TEST 9: Pre-filter")
    from orchestrator import _pre_filter

    check("MacBook Air passes", _pre_filter({"id": "1", "title": "MacBook Air M1", "price": 300}))
    check("macbook lowercase passes", _pre_filter({"id": "2", "title": "macbook pro 14", "price": 500}))
    check("No price → skip", not _pre_filter({"id": "3", "title": "MacBook Air M1", "price": None}))
    check("No macbook keyword → skip", not _pre_filter({"id": "4", "title": "Laptop Dell", "price": 300}))
    check("Case/cover → skip", not _pre_filter({"id": "5", "title": "MacBook case cover", "price": 10}))
    check("Case with model hint → passes", _pre_filter({"id": "6", "title": "MacBook Pro Air case M1", "price": 10}))
    check("Charger → skip", not _pre_filter({"id": "7", "title": "MacBook charger adapter", "price": 20}))
    check("Lithuanian dėklas → skip", not _pre_filter({"id": "8", "title": "MacBook dėklas", "price": 5}))
    check("M5 model hint passes", _pre_filter({"id": "9", "title": "MacBook Pro M5 2025", "price": 1500}))


# ════════════════════════════════════════════════════════════════════
# TEST 10: Launchd plist validation
# ════════════════════════════════════════════════════════════════════
def test_launchd():
    print("\n🚀 TEST 10: Launchd plist validation")
    import plistlib

    plist_path = "com.deal-finder.plist"
    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)

    check("Label is com.deal-finder", plist.get("Label") == "com.deal-finder")
    check("RunAtLoad is True", plist.get("RunAtLoad") is True)
    check("KeepAlive is True", plist.get("KeepAlive") is True)
    check("ThrottleInterval is 30", plist.get("ThrottleInterval") == 30)

    args = plist.get("ProgramArguments", [])
    check("Python path points to .venv", ".venv/bin/python" in args[0] if args else False)
    check("Script is orchestrator.py", "orchestrator.py" in args[1] if len(args) > 1 else False)
    check("Python exists", os.path.exists(args[0]) if args else False, f"path: {args[0] if args else '?'}")
    check("Script exists", os.path.exists(args[1]) if len(args) > 1 else False)

    wd = plist.get("WorkingDirectory", "")
    check("WorkingDirectory exists", os.path.isdir(wd), f"path: {wd}")

    check("Plist installed in LaunchAgents", os.path.exists(os.path.expanduser("~/Library/LaunchAgents/com.deal-finder.plist")))


# ════════════════════════════════════════════════════════════════════
# TEST 11: Live Vinted + AI filter integration
# ════════════════════════════════════════════════════════════════════
async def test_live_ai():
    print("\n🌐 TEST 11: Live Vinted → AI integration")
    from scrapers.vinted import poll as vinted_poll
    from filter import analyze_listing
    import re

    try:
        listings = await vinted_poll()
        check("Vinted poll succeeds", len(listings) > 0, f"got {len(listings)}")
    except Exception as e:
        check("Vinted poll succeeds", False, str(e))
        return

    # Find an M-series listing
    m_series = [l for l in listings if re.search(r'\bm[1-5]\b', l.get("title", "") + " " + (l.get("description", "") or ""), re.IGNORECASE)]

    if not m_series:
        # Test with any MacBook listing
        macbooks = [l for l in listings if re.search(r'macbook', l.get("title", ""), re.IGNORECASE) and l.get("price")]
        target = macbooks[0] if macbooks else listings[0]
        logger.info(f"No M-series found, testing with: {target['title'][:60]}")
    else:
        target = m_series[0]
        logger.info(f"Testing M-series: {target['title'][:60]}")

    start = time.time()
    result = await analyze_listing(target)
    elapsed = time.time() - start

    check("AI returns result (not None)", result is not None)
    if result is None:
        return

    check("AI response < 15s", elapsed < 15, f"took {elapsed:.1f}s")
    check("Result has verdict", "verdict" in result)
    check("Verdict is valid", result["verdict"] in ("SEND", "SEND_FLAGGED", "SKIP"))

    if result["verdict"] != "SKIP" or result.get("model_name"):
        check("Has model_name", result.get("model_name") is not None)
        check("Has verdict_reason", bool(result.get("verdict_reason")))

    if result.get("sell_price") is not None:
        check("Sell price is positive", result["sell_price"] > 0)
        check("Has cost breakdown", result.get("cost_breakdown") is not None)
        check("Total costs > purchase", result.get("total_costs", 0) >= target["price"])

    logger.info(f"  Result: {result['verdict']} | Model: {result.get('model_name')} | Profit: €{result.get('net_profit')}")


# ════════════════════════════════════════════════════════════════════
# TEST 12: Telegram connectivity
# ════════════════════════════════════════════════════════════════════
async def test_telegram():
    print("\n📨 TEST 12: Telegram connectivity")
    from telegram import Bot
    import config

    try:
        tbot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        me = await tbot.get_me()
        check("Bot auth works", me is not None, f"bot: {me.username}")
        check("Bot username exists", bool(me.username))
    except Exception as e:
        check("Bot auth works", False, str(e))


# ════════════════════════════════════════════════════════════════════
# RUN ALL
# ════════════════════════════════════════════════════════════════════
async def run_all():
    test_config()
    test_data_files()
    await test_db()
    test_price_lookup()
    test_cost_calculation()
    test_verdict()
    test_bot_formatting()
    test_listing_logger()
    test_pre_filter()
    test_launchd()
    await test_telegram()
    await test_live_ai()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 60}")
    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())
