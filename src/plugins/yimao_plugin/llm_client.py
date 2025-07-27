# yimao_plugin/llm_client.py
import asyncio
import httpx
import json
import logging
import datetime
from . import config, tools

logger = logging.getLogger("GeminiPlugin.client")

async def call_gemini_api(messages: list, system_prompt_content: str, model_to_use: str, use_tools: bool) -> dict:
    """【修正版】在发送API请求前，动态注入当前时间到系统提示词模板中"""
    api_url = f"{config.DEFAULT_API_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.DEFAULT_API_TOKEN}"}

    #时间注入逻辑
    formatted_system_prompt = system_prompt_content
    if system_prompt_content and "{current_time}" in system_prompt_content:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_system_prompt = system_prompt_content.format(current_time=now_str)
        logger.info(f"已向系统提示词注入当前时间: {now_str}")

    all_messages = []
    if formatted_system_prompt:
        all_messages.append({"role": "system", "content": formatted_system_prompt})

    #消息处理逻辑
    if not messages:
        # messages 为空
        all_messages.append({"role": "user", "content": "..."})
        logger.info("检测到空消息列表，添加占位符以触发AI开场白。")
    else:
        all_messages.extend(messages)

    payload = {
        "model": model_to_use,
        "messages": all_messages,
        "stream": False,
        "temperature": 0.75,
    }
    
    if use_tools:
        payload["tools"] = tools.tools_definition_openai
        payload["tool_choice"] = "auto"

    MAX_RETRIES = 3
    RETRY_DELAY = 5

    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"向LLM发送API请求 (尝试 {attempt + 1}/{MAX_RETRIES})...")
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(api_url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in [500, 503] and attempt < MAX_RETRIES - 1:
                logger.warning(f"API返回 {e.response.status_code} (服务器临时错误)，将在 {RETRY_DELAY} 秒后重试...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error(f"调用API时发生HTTP错误: {e.response.status_code} - {e.response.text}")
                raise
        except Exception as e:
            logger.error(f"调用API时发生未知错误: {e}", exc_info=True)
            if attempt < MAX_RETRIES -1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                return {"choices": [{"message": {"content": "喵呜~ 我的大脑好像被毛线缠住啦！"}}]}
    
    return {"choices": [{"message": {"content": "喵呜~ API持续繁忙或出错，请稍后再试吧！"}}]}


async def call_gemini_vision_api(prompt_text: str, image_base64: str) -> str:
    api_url = f"{config.DEFAULT_API_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.DEFAULT_API_TOKEN}"}
    
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]
    }]
    data = {
        "model": config.DEFAULT_MODEL_NAME,
        "messages": messages,
        "stream": False,
        "temperature": 0.75
    }

    logger.info(f"发送 Vision API 请求: {config.DEFAULT_MODEL_NAME}")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(api_url, headers=headers, json=data)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"调用 Vision API 时出错: {e}", exc_info=True)
        return "喵呜~ 我的视觉模块好像被毛线缠住啦！"