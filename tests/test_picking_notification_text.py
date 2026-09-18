from model.run_picking_date_alert import PickingDateSummary, RegionPickingSummary, build_notification_message


def region(name, status, suitable, hours, windows, rain, humidity, reasons):
    return RegionPickingSummary(
        region_name=name, picking_class=status, suitable=suitable,
        suitable_hour_count=hours, suitable_windows=windows,
        mean_suitable_fraction=0.5, mean_raining_fraction=0.1,
        mean_humid_fraction=0.2, mean_post_rain_dry_enough_fraction=0.6,
        total_rain_mm=rain, mean_temperature_c=24.0,
        mean_relative_humidity=humidity, mean_solar_radiation_wm2=220.0,
        reasons=reasons,
    )


def test_notification_uses_summary_detail_structure_and_excludes_outer_regions():
    summary = PickingDateSummary(
        target_date="2026-09-18", has_suitable_region=True,
        suitable_regions=["茶林区域-东部", "外围-东部"], checked_files=64, matched_files=64,
        region_summaries=[
            region("外围-东部", "适宜采茶", True, 7, ["09:00-15:00"], 0.0, 70.0, ["降雨、湿度和光照条件满足"]),
            region("茶林区域-东部", "适宜采茶", True, 6, ["09:00-14:00"], 0.0, 75.0, ["降雨、湿度和光照条件满足"]),
            region("茶林区域-南部", "暂不适宜采茶", False, 0, [], 6.2, 92.7, ["降雨覆盖比例54%", "高湿覆盖比例73%"]),
        ],
    )
    message = build_notification_message(summary)
    assert message == (
        "总结：2026-09-18适宜采茶的核心区域：茶林区域-东部。\n\n"
        "茶林区域-东部，适宜采茶；适采6小时；建议时段：09:00-14:00；"
        "预计雨量0.0毫米；平均湿度75.0%；说明：降雨、湿度和光照条件满足\n\n"
        "茶林区域-南部，暂不适宜采茶；适采0小时；建议时段：无；"
        "预计雨量6.2毫米；平均湿度92.7%；说明：降雨覆盖比例54%、高湿覆盖比例73%"
    )
    assert "外围-东部" not in message


def test_notification_empty_state_matches_app_wording():
    summary = PickingDateSummary("2026-09-18", False, [], 0, 0, [])
    assert build_notification_message(summary) == "采茶建议尚未计算。"
