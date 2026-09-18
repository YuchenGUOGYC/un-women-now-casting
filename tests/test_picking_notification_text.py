from model.run_picking_date_alert import PickingDateSummary, RegionPickingSummary, build_notification_message


def test_notification_uses_same_region_text_as_streamlit_app():
    region = RegionPickingSummary(
        region_name="外围-东部", picking_class="部分时段适宜", suitable=True,
        suitable_hour_count=4, suitable_windows=["09:00-12:00"],
        mean_suitable_fraction=0.48, mean_raining_fraction=0.10,
        mean_humid_fraction=0.20, mean_post_rain_dry_enough_fraction=0.60,
        total_rain_mm=1.2, mean_temperature_c=24.0,
        mean_relative_humidity=78.5, mean_solar_radiation_wm2=220.0,
        reasons=["降雨覆盖比例10%", "高湿覆盖比例20%"],
    )
    summary = PickingDateSummary(
        target_date="2026-09-18", has_suitable_region=True,
        suitable_regions=["外围-东部"], checked_files=64, matched_files=64,
        region_summaries=[region],
    )
    assert build_notification_message(summary) == (
        "外围-东部：部分时段适宜；适采4小时；建议时段：09:00-12:00；"
        "预计雨量1.2毫米；平均湿度78.5%；说明：降雨覆盖比例10%、高湿覆盖比例20%"
    )


def test_notification_empty_state_matches_app_wording():
    summary = PickingDateSummary("2026-09-18", False, [], 0, 0, [])
    assert build_notification_message(summary) == "采茶建议尚未计算。"
