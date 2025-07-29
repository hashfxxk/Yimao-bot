# yimao_plugin/tools.py
import asyncio
import httpx
import logging
import requests
from . import config

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger("GeminiPlugin.tools")


async def search_web(query: str) -> str:
    """使用 Google Programmable Search Engine 执行通用的网页搜索。"""
    logger.info(f"正在使用 Google API 执行网页搜索: {query}")
    try:
        def _sync_google_search():
            # 【全新实现，使用requests，代理支持更好】
            proxies = {}
            if config.HTTP_PROXY:
                # requests库能自动处理 http/https/socks 代理
                # 关键：SOCKS5代理需要写成 socks5h://
                proxy_url = f"socks5h://{config.HTTP_PROXY.split('//')[1]}"
                proxies['http'] = proxy_url
                proxies['https' ] = proxy_url
                logger.info(f"已配置代理: {proxies}")

            # Google API的URL
            api_url = "https://www.googleapis.com/customsearch/v1"
            
            params = {
                'key': config.GOOGLE_API_KEY,
                'cx': config.GOOGLE_CSE_ID,
                'q': query,
                'num': 5
            }

            response = requests.get(api_url, params=params, proxies=proxies, timeout=20)
            response.raise_for_status() # 如果状态码不是2xx，则抛出异常
            
            res = response.json()

            if not res.get('items'):
                return f"通过 Google 搜索“{query}”没有找到相关信息。"
            
            results = res['items']
            formatted_results = "\n".join(
                [f"- **{r['title']}**: {r.get('snippet', '无摘要')}" for r in results]
            )
            return f"这是关于“{query}”的Google搜索结果：\n{formatted_results}"

        result_str = await asyncio.to_thread(_sync_google_search)
        return result_str

    except requests.exceptions.ProxyError as e:
        logger.error(f"代理连接失败: {e}", exc_info=True)
        return "工具错误：无法连接到本地代理服务器，请检查代理软件是否开启或端口是否正确。"
    except requests.exceptions.Timeout:
        logger.error(f"请求Google API超时")
        return "工具错误：通过代理访问Google API超时，请检查代理节点是否通畅。"
    except requests.exceptions.RequestException as e:
        logger.error(f"请求Google API时发生网络错误: {e}", exc_info=True)
        if e.response is not None and e.response.status_code == 429:
             return "工具错误：今天使用 Google 搜索的次数已经用完啦，请明天再试吧！"
        return f"工具错误：Google搜索服务出现网络问题。"
    except Exception as e:
        logger.error(f"Google网页搜索失败: {e}", exc_info=True)
        return f"工具错误：网页搜索功能在执行查询“{query}”时遇到内部错误。"


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

available_tools = {
    "search_web": search_web,
    "search_weather": search_weather,
}

tools_definition_openai = [
    {
        "type": "function",
        "function": {
            "name": "search_web", 
            "description": "用于搜索互联网上的实时信息。当需要回答关于最新事件、新闻、事实、定义，或者任何用户提到的、你不确定的新词、新概念、'梗'时，都应该使用此工具。这是获取你知识库之外信息的主要方式。",
            "parameters": {
                "type": "object", 
                "properties": {
                    "query": {
                        "type": "string", 
                        "description": "要搜索的关键词或问题。例如：'今日头条' 或 '“Meme”是什么意思？'"
                    }
                }, 
                "required": ["query"]
            }
        }
    },
    {
        "type": "function", 
        "function": {
            "name": "search_weather", 
            "description": "查询指定城市的实时天气信息和未来7天的天气预报。",
            "parameters": {
                "type": "object", 
                "properties": {
                    "location": {
                        "type": "string", 
                        "description": "需要查询天气的城市名称，例如 '北京', '上海', 'London'。"
                    }
                }, 
                "required": ["location"]
            }
        }
    }
]