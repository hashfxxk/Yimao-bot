# yimao_plugin/tools.py
import asyncio
import httpx
import logging
import datetime
from ddgs import DDGS
from . import config

logger = logging.getLogger("GeminiPlugin.tools")

def get_current_time() -> str:
    """获取当前日期和时间"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

async def search_web(query: str) -> str:
    """执行通用的网页搜索。"""
    logger.info(f"正在执行通用网页搜索: {query}")
    try:
        def _sync_search():
            results = DDGS().text(query, max_results=5)
            if not results:
                return "没有找到相关信息。"
            formatted_results = "\n".join(
                [f"- **{r['title']}**: {r['body']}" for r in results]
            )
            return f"这是关于“{query}”的搜索结果：\n{formatted_results}"

        result_str = await asyncio.to_thread(_sync_search)
        return result_str

    except Exception as e:
        logger.error(f"网页搜索失败: {e}", exc_info=True)
        return f"工具错误：网页搜索功能在执行查询“{query}”时遇到网络问题或内部错误，无法获取结果。"

async def search_news(query: str) -> str:
    """通过一个精确的、硬编码的网页搜索查询来获取最新的新闻头条。"""
    effective_query = "今日最新国内国际新闻头条摘要"
    logger.info(f"新闻搜索请求被触发。忽略原始查询 '{query}'，使用硬编码的有效查询: '{effective_query}'")
    
    try:
        def _sync_search_news():
            with DDGS() as ddgs:
                results = list(ddgs.text(effective_query, region="cn-zh", max_results=7))
                if not results:
                    logger.error(f"致命错误：使用精确查询 '{effective_query}' 仍然无法通过通用搜索获取新闻。")
                    return "抱歉，搜索新闻的功能似乎暂时失效了。"
                return "\n\n".join([f"标题: {r['title']}\n链接: {r['href']}\n摘要: {r['body']}" for r in results])

        result_str = await asyncio.to_thread(_sync_search_news)
        return result_str
        
    except Exception as e:
        logger.error(f"在执行定向新闻搜索时发生严重错误: {e}", exc_info=True)
        return f"新闻搜索功能出现故障: {e}"


async def search_weather(location: str) -> str:
    """查询指定地点的实时天气和未来7天的天气预报。"""
    logger.info(f"正在为 '{location}' 查询天气(实时+7日预报)...")
    async with httpx.AsyncClient() as client:
        try:
            lookup_url = f"https://{config.QWEATHER_API_HOST}/geo/v2/city/lookup"
            params = {"location": location, "key": config.QWEATHER_API_KEY}
            resp_lookup = await client.get(lookup_url, params=params, timeout=10.0)
            resp_lookup.raise_for_status()
            data_lookup = resp_lookup.json()

            if data_lookup.get("code") != "200" or not data_lookup.get("location"):
                logger.warning(f"无法找到地点 '{location}' 的ID: {data_lookup}")
                return f"找不到地区 '{location}' 的天气信息，请换一个更具体的城市名称试试。"
            
            location_info = data_lookup["location"][0]
            location_id = location_info["id"]
            actual_city_name = f"{location_info.get('country', '')} {location_info.get('adm1', '')} {location_info.get('name', '')}".strip()
            logger.info(f"成功获取地点ID: {location_id} for {actual_city_name}")

            async def get_now():
                url = f"https://{config.QWEATHER_API_HOST}/v7/weather/now"
                params = {"location": location_id, "key": config.QWEATHER_API_KEY}
                return await client.get(url, params=params, timeout=10.0)

            async def get_7d_forecast():
                url = f"https://{config.QWEATHER_API_HOST}/v7/weather/7d"
                params = {"location": location_id, "key": config.QWEATHER_API_KEY}
                return await client.get(url, params=params, timeout=10.0)

            responses = await asyncio.gather(get_now(), get_7d_forecast())
            for resp in responses: resp.raise_for_status()
            
            data_now_raw, data_7d_raw = responses[0].json(), responses[1].json()
            
            now_result = "实时天气获取失败。"
            if data_now_raw.get("code") == "200" and data_now_raw.get("now"):
                now = data_now_raw["now"]
                now_result = f"天气: {now.get('text', 'N/A')}\n体感温度: {now.get('feelsLike', 'N/A')}°C (实际: {now.get('temp', 'N/A')}°C)\n风: {now.get('windDir', 'N/A')} {now.get('windScale', 'N/A')}级\n湿度: {now.get('humidity', 'N/A')}% | 压强: {now.get('pressure', 'N/A')}hPa"

            forecast_result = "天气预报获取失败。"
            if data_7d_raw.get("code") == "200" and data_7d_raw.get("daily"):
                forecasts = [f"  - {day['fxDate']}: {day['textDay']}转{day['textNight']}, 温度 {day['tempMin']}~{day['tempMax']}°C, 紫外线{day['uvIndex']}级" for day in data_7d_raw["daily"]]
                forecast_result = "【未来7日天气预报】\n" + "\n".join(forecasts)
            
            update_time_str = data_now_raw.get("updateTime", "未知").replace("T", " ").replace("+08:00", "")
            return f"查询地点: {actual_city_name}\n更新时间: {update_time_str}\n--------------------\n【实时天气】\n{now_result}\n--------------------\n{forecast_result}".strip()

        except httpx.HTTPStatusError as e:
            logger.error(f"天气API请求失败 (HTTP状态码): {e.response.status_code} - {e.response.text}")
            return f"天气服务出现网络问题 (HTTP {e.response.status_code})。"
        except Exception as e:
            logger.error(f"查询天气时发生未知错误: {e}", exc_info=True)
            return f"查询天气时发生了未知错误: {e}"

# 注册所有可用工具
available_tools = {
    "get_current_time": get_current_time,
    "search_web": search_web,
    "search_news": search_news,
    "search_weather": search_weather,
}

# 向AI模型声明工具的定义
tools_definition_openai = [
    {"type": "function", "function": {"name": "get_current_time", "description": "获取当前日期和时间", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "search_web", "description": "搜索通用的网页信息，用于回答事实性、知识性的问题。", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "search_news", "description": "获取最新的新闻头条和时事。当用户询问‘今日头条’、‘最新新闻’时，应使用此工具。", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "此参数可以忽略，函数会自动获取新闻。"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "search_weather", "description": "查询指定城市的实时天气信息和未来7天的天气预报。当用户询问未来天气（如“明天天气怎么样”）时，应使用此工具。", "parameters": {"type": "object", "properties": {"location": {"type": "string", "description": "需要查询天气的城市名称，例如 '北京', '上海', 'London' 等。"}}, "required": ["location"]}}}
]