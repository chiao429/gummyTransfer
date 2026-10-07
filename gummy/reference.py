"""Category names from the user's reference workbook, without its historical metrics."""

REFERENCE_CATEGORIES = (
    "六角扳手(Hex Key Wrench)",
    "羊角鎚(Claw Hammer)",
    "扭力扳手1/2(Torque Wrench 1/2)",
    "活動扳手(Adjustable Wrench)",
    "棘輪扳手(Ratchet Wrench)",
    "數位扭力扳手(Digital Torque Wrench)",
    "鐵皮剪(Tin Snips)",
    "水煮麵(Stewed Noodles)",
    "巧克力(Chocolate)",
    "高蛋白營養棒(Protein Bars)",
    "行車紀錄器含後視鏡整合式(DVR)",
    "後視鏡式行車紀錄器(DVR-E-Mirror)",
    "吉他效果器(Guitar Effects)",
    "自動給皂機(Commercial Soap Dispensers)",
    "空氣清淨機(Air Purifier)",
    "門禁讀卡機(Access Control Keypad)",
    "個人掌上型空氣淨化器(Portable Air Purifier)",
    "掃地機器人(Robot Vacuum)",
    "混音器(Audio Mixer)",
    "麥克風混音器(Microphone Mixer)",
    "藍芽喇叭(Bluetooth Speaker)",
    "男性機能內褲(Performance Underwear for Men)",
)


def reference_coverage(stats: dict[str, dict], source: str) -> dict:
    """Show sampled snapshot counts without treating them as market coverage."""

    categories = [
        {
            "category": name,
            **stats.get(name, {"run_count": 0, "distinct_product_count": 0, "latest_run_id": None}),
        }
        for name in REFERENCE_CATEGORIES
    ]
    outside = [
        {"category": name, **values}
        for name, values in sorted(stats.items())
        if name not in REFERENCE_CATEGORIES
    ]
    return {
        "source": source,
        "reference_total": len(REFERENCE_CATEGORIES),
        "sampled_count": sum(row["run_count"] > 0 for row in categories),
        "categories": categories,
        "outside_reference": outside,
        "scope": "successful_snapshots_only_not_market_coverage",
    }
