"""Built-in weather query tool.

Risk level: low (read-only, no side effects).
Uses wttr.in as the free weather data source — no API key required.
Supports mainland China, Hong Kong, Macau, and Taiwan cities.
"""

from __future__ import annotations

from datetime import date as _date, datetime, timedelta
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── City name aliases ────────────────────────────────────────────────────────
# Maps Chinese city names (and variants) to wttr.in query format.
# wttr.in accepts: English names, Chinese characters, pinyin, airport codes.

CITY_ALIASES: dict[str, str] = {
    # ── Municipalities ──
    "北京": "Beijing",
    "北京市": "Beijing",
    "beijing": "Beijing",
    "上海": "Shanghai",
    "上海市": "Shanghai",
    "shanghai": "Shanghai",
    "天津": "Tianjin",
    "天津市": "Tianjin",
    "tianjin": "Tianjin",
    "重庆": "Chongqing",
    "重庆市": "Chongqing",
    "chongqing": "Chongqing",
    # ── Special Administrative Regions ──
    "香港": "Hong+Kong",
    "香港特别行政区": "Hong+Kong",
    "hongkong": "Hong+Kong",
    "hong kong": "Hong+Kong",
    "澳门": "Macau",
    "澳门特别行政区": "Macau",
    "macau": "Macau",
    "macao": "Macau",
    # ── Taiwan ──
    "台北": "Taipei",
    "台北市": "Taipei",
    "taipei": "Taipei",
    "臺北": "Taipei",
    "臺北市": "Taipei",
    "高雄": "Kaohsiung",
    "高雄市": "Kaohsiung",
    "kaohsiung": "Kaohsiung",
    "台中": "Taichung",
    "台中市": "Taichung",
    "臺中": "Taichung",
    "臺中市": "Taichung",
    "taichung": "Taichung",
    "台南": "Tainan",
    "台南市": "Tainan",
    "臺南": "Tainan",
    "臺南市": "Tainan",
    "tainan": "Tainan",
    "桃园": "Taoyuan",
    "桃園": "Taoyuan",
    "taoyuan": "Taoyuan",
    # ── Provincial Capitals ──
    "石家庄": "Shijiazhuang",
    "石家庄市": "Shijiazhuang",
    "太原": "Taiyuan",
    "太原市": "Taiyuan",
    "呼和浩特": "Hohhot",
    "呼和浩特市": "Hohhot",
    "沈阳": "Shenyang",
    "沈阳市": "Shenyang",
    "长春": "Changchun",
    "长春市": "Changchun",
    "哈尔滨": "Harbin",
    "哈尔滨市": "Harbin",
    "南京": "Nanjing",
    "南京市": "Nanjing",
    "杭州": "Hangzhou",
    "杭州市": "Hangzhou",
    "合肥": "Hefei",
    "合肥市": "Hefei",
    "福州": "Fuzhou",
    "福州市": "Fuzhou",
    "南昌": "Nanchang",
    "南昌市": "Nanchang",
    "济南": "Jinan",
    "济南市": "Jinan",
    "郑州": "Zhengzhou",
    "郑州市": "Zhengzhou",
    "武汉": "Wuhan",
    "武汉市": "Wuhan",
    "长沙": "Changsha",
    "长沙市": "Changsha",
    "广州": "Guangzhou",
    "广州市": "Guangzhou",
    "南宁": "Nanning",
    "南宁市": "Nanning",
    "海口": "Haikou",
    "海口市": "Haikou",
    "成都": "Chengdu",
    "成都市": "Chengdu",
    "贵阳": "Guiyang",
    "贵阳市": "Guiyang",
    "昆明": "Kunming",
    "昆明市": "Kunming",
    "拉萨": "Lhasa",
    "拉萨市": "Lhasa",
    "西安": "Xian",
    "西安市": "Xian",
    "兰州": "Lanzhou",
    "兰州市": "Lanzhou",
    "西宁": "Xining",
    "西宁市": "Xining",
    "银川": "Yinchuan",
    "银川市": "Yinchuan",
    "乌鲁木齐": "Urumqi",
    "乌鲁木齐市": "Urumqi",
    # ── Major Cities ──
    "深圳": "Shenzhen",
    "深圳市": "Shenzhen",
    "苏州": "Suzhou",
    "苏州市": "Suzhou",
    "无锡": "Wuxi",
    "无锡市": "Wuxi",
    "宁波": "Ningbo",
    "宁波市": "Ningbo",
    "青岛": "Qingdao",
    "青岛市": "Qingdao",
    "大连": "Dalian",
    "大连市": "Dalian",
    "厦门": "Xiamen",
    "厦门市": "Xiamen",
    "珠海": "Zhuhai",
    "珠海市": "Zhuhai",
    "东莞": "Dongguan",
    "东莞市": "Dongguan",
    "佛山": "Foshan",
    "佛山市": "Foshan",
    "三亚": "Sanya",
    "三亚市": "Sanya",
    "桂林": "Guilin",
    "桂林市": "Guilin",
    "丽江": "Lijiang",
    "丽江市": "Lijiang",
    "大理": "Dali",
    "大理市": "Dali",
}

# ── Weather condition Chinese translations ───────────────────────────────────

WEATHER_CODE_ZH: dict[str, str] = {
    "113": "晴",
    "116": "多云",
    "119": "阴",
    "122": "阴",
    "143": "雾",
    "176": "局部阵雨",
    "179": "局部雪",
    "182": "局部雨夹雪",
    "185": "局部冻雨",
    "200": "雷阵雨",
    "227": "暴风雪",
    "230": "暴风雪",
    "248": "雾",
    "260": "冻雾",
    "263": "小雨",
    "266": "小雨",
    "281": "冻雨",
    "284": "冻雨",
    "293": "局部小雨",
    "296": "小雨",
    "299": "中雨",
    "302": "中雨",
    "305": "大雨",
    "308": "大雨",
    "311": "冻雨",
    "314": "冻雨",
    "317": "雨夹雪",
    "320": "雨夹雪",
    "323": "小雪",
    "326": "小雪",
    "329": "中雪",
    "332": "中雪",
    "335": "大雪",
    "338": "大雪",
    "350": "冰雹",
    "353": "局部阵雨",
    "356": "中雨",
    "359": "大雨",
    "362": "雨夹雪",
    "365": "雨夹雪",
    "368": "小雪",
    "371": "大雪",
    "374": "冰雹",
    "377": "冰雹",
    "386": "局部雷阵雨",
    "389": "雷阵雨",
    "392": "局部雷雪",
    "395": "大雪",
}

WIND_DIR_ZH: dict[str, str] = {
    "N": "北风",
    "NNE": "东北偏北风",
    "NE": "东北风",
    "ENE": "东北偏东风",
    "E": "东风",
    "ESE": "东南偏东风",
    "SE": "东南风",
    "SSE": "东南偏南风",
    "S": "南风",
    "SSW": "西南偏南风",
    "SW": "西南风",
    "WSW": "西南偏西风",
    "W": "西风",
    "WNW": "西北偏西风",
    "NW": "西北风",
    "NNW": "西北偏北风",
}


def _normalize_city(city: str) -> str:
    """Normalize city name to wttr.in query format.

    Checks CITY_ALIASES first, then passes through as-is for English/pinyin names.
    """
    if city in CITY_ALIASES:
        return CITY_ALIASES[city]
    # Also try stripping "市" / "县" / "区" suffix
    for suffix in ("市", "县", "区", "镇"):
        stripped = city.removesuffix(suffix)
        if stripped in CITY_ALIASES:
            return CITY_ALIASES[stripped]
    # Pass through as-is (wttr.in handles pinyin and English)
    return city


def _translate_weather_code(code: str) -> str:
    """Translate wttr.in weather code to Chinese description."""
    return WEATHER_CODE_ZH.get(code, f"未知({code})")


def _translate_wind_direction(direction: str) -> str:
    """Translate wind direction abbreviation to Chinese."""
    return WIND_DIR_ZH.get(direction, direction)


def _resolve_date(target_date: str | None) -> tuple[str, int]:
    """Resolve a date string to (label, days_from_today).

    Returns:
        ("today", 0), ("tomorrow", 1), ("day2", 2), ("day3", 3), or ("past", -1) / ("far_future", +N).
    """
    today = _date.today()

    if not target_date:
        return ("today", 0)

    try:
        parsed = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"日期格式无效: {target_date}，请使用 YYYY-MM-DD 格式")

    delta = (parsed - today).days

    if delta < 0:
        return ("past", delta)
    elif delta == 0:
        return ("today", 0)
    elif delta == 1:
        return ("tomorrow", 1)
    elif delta == 2:
        return ("day_after_tomorrow", 2)
    elif delta == 3:
        return ("day3", 3)
    else:
        return ("far_future", delta)


def _extract_current_weather(data: dict[str, Any]) -> dict[str, Any]:
    """Extract current weather conditions from wttr.in JSON response."""
    current = data.get("current_condition", [{}])[0]
    return {
        "observation_time": current.get("observation_time", ""),
        "temperature_c": current.get("temp_C", ""),
        "feels_like_c": current.get("FeelsLikeC", ""),
        "humidity": current.get("humidity", ""),
        "weather_code": current.get("weatherCode", ""),
        "weather_desc": _translate_weather_code(current.get("weatherCode", "")),
        "weather_desc_en": current.get("weatherDesc", [{}])[0].get("value", ""),
        "wind_speed_kmh": current.get("windspeedKmph", ""),
        "wind_direction": current.get("winddir16Point", ""),
        "wind_direction_zh": _translate_wind_direction(current.get("winddir16Point", "")),
        "wind_degree": current.get("winddirDegree", ""),
        "visibility_km": current.get("visibility", ""),
        "pressure_hpa": current.get("pressure", ""),
        "uv_index": current.get("uvIndex", ""),
        "cloud_cover": current.get("cloudcover", ""),
    }


def _extract_day_forecast(day_data: dict[str, Any], day_label: str) -> dict[str, Any]:
    """Extract a single day's forecast from wttr.in JSON response."""
    astronomy = day_data.get("astronomy", [{}])[0]
    hourly = day_data.get("hourly", [])

    # Aggregate hourly data
    temps = []
    for h in hourly:
        try:
            temps.append(int(h.get("tempC", 0)))
        except (ValueError, TypeError):
            pass

    return {
        "date": day_data.get("date", ""),
        "day_label": day_label,
        "temp_max_c": day_data.get("maxtempC", ""),
        "temp_min_c": day_data.get("mintempC", ""),
        "temp_avg_c": day_data.get("avgtempC", ""),
        "sun_hours": day_data.get("sunHour", ""),
        "uv_index": day_data.get("uvIndex", ""),
        "sunrise": astronomy.get("sunrise", ""),
        "sunset": astronomy.get("sunset", ""),
        "moonrise": astronomy.get("moonrise", ""),
        "moonset": astronomy.get("moonset", ""),
        "moon_phase": astronomy.get("moon_phase", ""),
        "hourly_summary": [
            {
                "time": h.get("time", "0"),
                "temp_c": h.get("tempC", ""),
                "weather_desc": _translate_weather_code(h.get("weatherCode", "")),
                "weather_desc_en": h.get("weatherDesc", [{}])[0].get("value", ""),
                "wind_speed_kmh": h.get("windspeedKmph", ""),
                "wind_direction_zh": _translate_wind_direction(h.get("winddir16Point", "")),
                "humidity": h.get("humidity", ""),
                "chance_of_rain": h.get("chanceofrain", ""),
            }
            for h in hourly[:8]  # Limit to 8 time slots for readability
        ],
    }


async def query_weather(
    city: str,
    date: str | None = None,
    preview: bool = False,
    **kwargs,
) -> dict[str, Any]:
    """Query weather for a city, optionally filtered by date.

    Supports mainland China, Hong Kong, Macau, and Taiwan cities.
    Uses wttr.in free API — covers current conditions and 3-day forecast.

    Args:
        city: City name in Chinese or English (e.g., "北京", "上海", "Hong Kong").
        date: Optional date in YYYY-MM-DD format. Defaults to today.
              Supports today and up to 3 days in the future.
              Historical dates are not supported by the free data source.
        preview: If True, return a preview of the query without fetching.

    Returns:
        Dict with weather data or error information.
    """
    if preview:
        return {
            "success": True,
            "preview": True,
            "city": city,
            "date": date or "today",
            "note": "Will query wttr.in for current weather and forecast",
        }

    # Normalize city name
    normalized_city = _normalize_city(city.strip())

    # Resolve date
    try:
        day_label, day_offset = _resolve_date(date)
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "invalid_date",
        }

    # Check date range
    if day_offset < 0:
        return {
            "success": False,
            "error": (
                f"无法查询 {date if date else '过去'} 的历史天气数据。"
                "wttr.in 免费接口仅支持今日实时天气和未来3天预报。"
                "如需历史天气数据，请配置和风天气(QWeather) API Key。"
                "设置环境变量 QWEATHER_API_KEY 后可使用历史天气查询。"
            ),
            "error_type": "historical_not_supported",
        }

    if day_offset > 3:
        return {
            "success": False,
            "error": (
                f"无法查询 {date} 的天气。wttr.in 免费接口仅支持今日及未来3天预报。"
                f"该日期距离今天还有 {day_offset} 天。"
                "如需更长期预报，请配置和风天气(QWeather) API Key。"
            ),
            "error_type": "forecast_too_far",
        }

    # Fetch weather from wttr.in
    try:
        import httpx

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(
                f"https://wttr.in/{normalized_city}",
                params={"format": "j1"},
                headers={"User-Agent": "Athena/0.1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("weather_http_error", city=city, normalized=normalized_city, error=str(e))
        return {
            "success": False,
            "error": f"天气服务暂时不可用，请稍后重试。({e})",
            "error_type": "http_error",
        }
    except Exception as e:
        logger.error("weather_unexpected_error", city=city, error=str(e))
        return {
            "success": False,
            "error": f"查询天气时发生未知错误: {e}",
            "error_type": "unknown_error",
        }

    # Build response
    # wttr.in "weather" array: [today, tomorrow, day3]
    weather_days = data.get("weather", [])

    if not weather_days:
        return {
            "success": False,
            "error": f"未找到城市「{city}」的天气数据。请检查城市名称是否正确。",
            "error_type": "city_not_found",
        }

    # Current conditions (always include)
    current = _extract_current_weather(data)

    # Nearest area / location info
    nearest = data.get("nearest_area", [{}])[0]

    result: dict[str, Any] = {
        "success": True,
        "city": city,
        "normalized_city": normalized_city,
        "query_date": date or str(_date.today()),
        "location": {
            "area_name": nearest.get("areaName", [{}])[0].get("value", ""),
            "country": nearest.get("country", [{}])[0].get("value", ""),
            "region": nearest.get("region", [{}])[0].get("value", ""),
            "latitude": nearest.get("latitude", ""),
            "longitude": nearest.get("longitude", ""),
        },
        "current": current,
    }

    # Add forecast for the requested day (or all days if no date specified)
    day_labels = ["today", "tomorrow", "day_after_tomorrow"]
    if day_offset == 0 or not date:
        # Include all available forecast days
        forecasts = []
        for i, day_data in enumerate(weather_days[:3]):
            label = day_labels[i] if i < len(day_labels) else f"day_{i}"
            forecasts.append(_extract_day_forecast(day_data, label))
        result["forecast"] = forecasts
    else:
        # Specific future day
        idx = day_offset - 1  # day 1 = index 0 in weather array
        if idx < len(weather_days):
            result["forecast"] = [_extract_day_forecast(weather_days[idx], day_label)]
        else:
            result["forecast"] = []

    result["source"] = "wttr.in"

    return result
