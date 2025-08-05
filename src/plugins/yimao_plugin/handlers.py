# yimao_plugin/handlers.py

import asyncio
import json
import logging
import os
import random
import shutil
import re
import httpx
import datetime
import time
import base64
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from typing import Literal, List, Dict, Any

from jmcomic import create_option_by_file, download_album, JmcomicClient
from jmcomic.jm_exception import MissingAlbumPhotoException, PartialDownloadFailedException

from nonebot import on_message
from nonebot.rule import Rule
from nonebot.matcher import Matcher
from nonebot.typing import T_State
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.adapters.onebot.v11 import Bot, Event, Message, GroupMessageEvent, MessageSegment

from . import config, data_store, llm_client, tools, utils

logger = logging.getLogger("GeminiPlugin.handlers")

DownloadResult = Literal["ok", "not_found", "error"]

# 【修改】上下文压缩函数现在需要传递模型名称
async def build_api_messages_with_compression(history: List[Dict[str, Any]], summary_model_for_new_images: str) -> List[Dict[str, Any]]:
    """
    遍历完整的对话历史，构建一个用于API请求的、经过压缩的上下文。
    为新图片生成摘要时，使用指定的 summary_model_for_new_images。
    """
    api_messages = []
    for record in history:
        processed_record = record.copy() 
        content = processed_record.get("content")

        if isinstance(content, list):
            new_content_parts = []
            has_image_to_process = False

            for item in content:
                if item.get("type") == "image_url":
                    if "summary" in item:
                        summary_text = f"[图片描述: {item['summary']}]"
                        new_content_parts.append({"type": "text", "text": summary_text})
                    else:
                        new_content_parts.append(item)
                        has_image_to_process = True
                else:
                    new_content_parts.append(item)
            
            processed_record["content"] = new_content_parts

            if has_image_to_process:
                for original_item in content: # 只遍历原始记录
                    if original_item.get("type") == "image_url" and "summary" not in original_item:
                        image_url = original_item.get("image_url", {}).get("url", "")
                        if image_url.startswith("data:image/jpeg;base64,"):
                            b64_data = image_url.split(",")[1]
                            logger.info(f"正在为新图片生成摘要，使用模型: {summary_model_for_new_images}")
                            # 【关键】传入指定的模型
                            summary = await llm_client.summarize_image_content(b64_data, model_to_use=summary_model_for_new_images)
                            original_item["summary"] = summary
                            
                            # 更新本次要发送的上下文，将图片替换为摘要
                            for i, part in enumerate(new_content_parts):
                                if part.get("type") == "image_url" and part.get("image_url") == original_item.get("image_url"):
                                    new_content_parts[i] = {"type": "text", "text": f"[图片描述: {summary}]"}
                                    break
                            logger.info("图片摘要已生成并替换了上下文中的图片。")
        api_messages.append(processed_record)
    return api_messages

# ... (run_jm_download_task, handle_random_jm 等函数保持不变) ...
async def run_jm_download_task(bot: Bot, event: Event, album_id: str) -> DownloadResult:
    # ...
    option_path = config.PROJECT_ROOT_DIR / "jm_option.yml"
    if not option_path.exists():
        logger.error("致命错误：JmComic配置文件 `jm_option.yml` 不存在！")
        return "error"
    base_dir = config.PROJECT_ROOT_DIR / "data" / "jmcomic"
    try:
        import yaml
        with open(option_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
            base_dir_from_cfg = cfg.get('dir_rule', {}).get('base_dir', str(base_dir))
            base_dir = Path(os.path.expanduser(base_dir_from_cfg))
    except Exception: pass
    final_pdf_path = base_dir / f"jm_{album_id}_0.pdf"
    temp_photo_dir = base_dir / album_id
    if final_pdf_path.exists():
        try: os.remove(final_pdf_path)
        except OSError as e: logger.error(f"清理旧PDF失败: {e}")
    if temp_photo_dir.exists():
        shutil.rmtree(temp_photo_dir, ignore_errors=True)
    final_photo_dir = temp_photo_dir
    try:
        option = create_option_by_file(str(option_path))
        client: JmcomicClient = option.build_jm_client()
        logger.info(f"开始使用安全模式下载禁漫 {album_id}...")
        await asyncio.to_thread(download_album, album_id, option)
        if not final_pdf_path.exists():
            if temp_photo_dir.exists(): shutil.rmtree(temp_photo_dir, ignore_errors=True)
            raise MissingAlbumPhotoException(f"PDF文件 {final_pdf_path} 未生成")
        try:
            album_detail = await asyncio.to_thread(client.get_album_detail, album_id)
            title = album_detail.title
            if title:
                invalid_chars = r'[\\/:*?"<>|]'
                safe_title = re.sub(invalid_chars, '_', title).strip()
                if safe_title:
                    new_path = base_dir / safe_title
                    if not new_path.exists() and temp_photo_dir.exists():
                        os.rename(temp_photo_dir, new_path)
                        final_photo_dir = new_path
        except Exception as rename_e:
            logger.error(f"获取标题或重命名文件夹时出错: {rename_e}")
        logger.info(f"开始上传文件 {final_pdf_path.name}...")
        api_to_call = "upload_group_file" if isinstance(event, GroupMessageEvent) else "upload_private_file"
        absolute_file_path = str(final_pdf_path.resolve())
        params = {"file": absolute_file_path, "name": final_pdf_path.name}
        if isinstance(event, GroupMessageEvent): params["group_id"] = event.group_id
        else: params["user_id"] = event.user_id
        await bot.call_api(api_to_call, **params, timeout=1800)
        logger.info(f"文件 {final_pdf_path.name} 上传成功。")
        try: 
            await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10024')
        except: pass
        return "ok"
    except (MissingAlbumPhotoException, PartialDownloadFailedException):
        logger.warning(f"ID {album_id} 下载失败")
        return "not_found"
    except Exception as e:
        logger.error(f"处理禁漫 {album_id} 时发生未知错误: {e}", exc_info=True)
        await bot.send(event, f"处理禁漫 {album_id} 时发生未知错误: {e}")
        try:
            await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
        except: pass
        return "error"
    finally:
        if final_photo_dir and final_photo_dir.exists():
            shutil.rmtree(final_photo_dir, ignore_errors=True)
        elif temp_photo_dir.exists():
            shutil.rmtree(temp_photo_dir, ignore_errors=True)
        if final_pdf_path.exists():
            try: os.remove(final_pdf_path)
            except Exception as e: logger.error(f"清理最终PDF文件时出错: {e}")


async def handle_random_jm(bot: Bot, event: Event, matcher: Matcher):
    max_retries = 10
    try:
        await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
    except Exception as e:
        logger.warning(f"为随机JM设置初始Emoji时失败: {e}")
    for i in range(max_retries):
        random_id = str(random.randint(1, 1500000))
        logger.info(f"随机JM尝试 #{i + 1}: 正在尝试ID {random_id}...")
        result: DownloadResult = await run_jm_download_task(bot, event, random_id)
        if result == "ok" or result == "error":
            logger.info(f"随机JM任务结束，状态: {result}")
            return
        elif result == "not_found":
            logger.info(f"ID {random_id} 未找到，将在1秒后重试...")
            await asyncio.sleep(1)
    logger.error(f"在尝试了 {max_retries} 次后，仍未找到有效的随机JM本子。")
    await matcher.send(f"喵呜~ 找了 {max_retries} 次都没找到存在的本子，今天运气不太好呢，要不你再试一次？")
    try:
        await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
    except: pass


# 【修改】主聊天会话现在决定为新图片使用哪个模型来生成摘要
async def handle_chat_session(bot: Bot, matcher: Matcher, event: MessageEvent, user_message_content: Any):
    session_id = event.get_session_id()
    if isinstance(event, GroupMessageEvent):
        try: await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        except: pass
    
    history_record_for_user = {"role": "user", "content": user_message_content, "message_id": event.message_id}
    
    prompt_text = ""
    if isinstance(user_message_content, str): prompt_text = user_message_content
    elif isinstance(user_message_content, list):
        prompt_text = next((item.get('text', '') for item in user_message_content if item.get('type') == 'text'), "")

    is_slash_mode = prompt_text.lstrip().startswith('/')
    mode = "slash" if is_slash_mode else "normal"
    
    data_store.update_slot_summary_if_needed(session_id, mode, prompt_text)
    
    history = data_store.get_active_history(session_id, mode)
    history.append(history_record_for_user)

    try:
        # 【关键】普通对话中，使用最强模型来分析图片
        messages_for_api = await build_api_messages_with_compression(list(history), summary_model_for_new_images=config.DEFAULT_MODEL_NAME)
    except Exception as e:
        logger.error(f"构建压缩上下文时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 我在整理记忆的时候出错了，请检查后台日志。")
        history.pop()
        return
        
    # ... (后续的 API 调用和响应处理逻辑保持不变) ...
    now_ts_str = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    if mode == "slash":
        model, system_prompt, use_function_calling = config.SLASH_COMMAND_MODEL_NAME, "", False
        if len(messages_for_api) == 1:
            first_turn_payload = messages_for_api[0]
            if isinstance(first_turn_payload.get('content'), str):
                original_content = first_turn_payload['content']
                first_turn_payload['content'] = f"{config.SLASH_COMMAND_SYSTEM_PROMPT}\n\n---\n\n{original_content.lstrip('/')}"
            elif isinstance(first_turn_payload.get('content'), list):
                 for item in first_turn_payload['content']:
                     if item.get('type') == 'text':
                         original_content = item['text']
                         item['text'] = f"{config.SLASH_COMMAND_SYSTEM_PROMPT}\n\n---\n\n{original_content.lstrip('/')}"
                         break
    else: 
        model, use_function_calling = config.DEFAULT_MODEL_NAME, True
        for msg in messages_for_api:
            if msg.get('role') in ['user', 'assistant']:
                content = msg.get('content', '')
                if isinstance(content, str) and not content.startswith('['): msg['content'] = now_ts_str + content
                elif isinstance(content, list):
                    text_part = next((p for p in content if p.get('type') == 'text'), None)
                    if text_part and not text_part['text'].startswith('['): text_part['text'] = now_ts_str + text_part['text']

        if isinstance(event, GroupMessageEvent) and str(event.group_id) in config.EMOTIONLESS_PROMPT_GROUP_IDS:
            system_prompt = config.EMOTIONLESS_SYSTEM_PROMPT
        else:
            system_prompt = config.DEFAULT_SYSTEM_PROMPT_TEMPLATE        

    logger.info(f"会话 {session_id} (模式: {mode}) 收到请求。")
    
    try:
        max_turns = 5
        for _ in range(max_turns):
            api_response = await llm_client.call_gemini_api(messages_for_api, system_prompt, model, use_function_calling)
            if "error" in api_response:
                error_msg_from_api = api_response["error"].get("message", "发生未知错误")
                await matcher.send(f"喵呜~ API出错了: {error_msg_from_api}")
                if history: history.pop()
                break
            
            response_message = api_response["choices"][0]["message"]
            
            if response_message.get("tool_calls"):
                assistant_message = {"role": "assistant", "content": response_message.get("content"), "tool_calls": response_message["tool_calls"]}
                messages_for_api.append(assistant_message)
                history.append(assistant_message) 

                for tool_call in response_message["tool_calls"]:
                    function_name = tool_call["function"]["name"]
                    function_args = json.loads(tool_call["function"].get("arguments", "{}"))
                    if function_name in tools.available_tools:
                        function_to_call = tools.available_tools[function_name]
                        tool_output = await function_to_call(**function_args)
                        messages_for_api.append({"tool_call_id": tool_call["id"], "role": "tool", "name": function_name, "content": tool_output})
                        history.append({"tool_call_id": tool_call["id"], "role": "tool", "name": function_name, "content": tool_output})
                continue
            else:
                response_content = response_message.get("content", "")
                if isinstance(event, GroupMessageEvent) and response_content:
                    bot_name = "Loki" if mode == "slash" else (await bot.get_login_info())['nickname'] or "一猫"
                    data_store.get_group_history(str(event.group_id)).append({ "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": bot.self_id, "user_name": bot_name, "content": response_content, "is_bot": True})
                
                assistant_message_payload = {"role": "assistant", "content": response_content}
                sent_msg_receipt = None
                if len(response_content) > config.FORWARD_TRIGGER_THRESHOLD:
                    bot_name = "Loki" if mode == "slash" else "一猫"
                    sent_msg_receipt = await utils.send_long_message_as_forward(bot, event, response_content, bot_name)
                elif response_content:
                    sent_msg_receipt = await matcher.send(Message(response_content))
                else:
                    await matcher.send("喵~ 我好像没什么好说的...")

                if sent_msg_receipt and 'message_id' in sent_msg_receipt:
                    assistant_message_payload['message_id'] = int(sent_msg_receipt['message_id'])
                    assistant_message_payload['response_to_id'] = event.message_id
                
                history.append(assistant_message_payload)
                break
        else:
            await matcher.send("喵呜~ 我思考得太久了...")
        
        if "error" not in locals().get("api_response", {}):
            data_store.save_memory_to_file()

        if isinstance(event, GroupMessageEvent):
            try:
                await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
                await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10024')
            except: pass
            
    except Exception as e:
        if history and history[-1] is history_record_for_user:
            history.pop()
        logger.error(f"处理聊天时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 我的大脑好像被毛线缠住啦！请检查后台日志。")


# ... (handle_memory_command, handle_challenge_chat, update_summary_for_group, is_bilibili_card 等函数保持不变) ...
async def handle_memory_command(matcher: Matcher, event: Event, args: Message = CommandArg()):
    # ...
    session_id = event.get_session_id()
    command = event.get_plaintext().strip()
    mode = "slash" if command.startswith('//') else "normal"
    arg_str = args.extract_plain_text().strip()
    if not arg_str:
        summary_text = data_store.get_memory_summary_list(session_id, mode)
        await matcher.send(summary_text)
    else:
        try:
            slot_num = int(arg_str)
            success, message = data_store.set_active_slot(session_id, mode, slot_num - 1)
            await matcher.send(message)
            if success:
                data_store.save_memory_to_file()
        except ValueError:
            await matcher.send("无效的指令。请输入数字编号。")

async def handle_challenge_chat(bot: Bot, matcher: Matcher, event: Event):
    # ...
    if str(event.user_id) in config.USER_BLACKLIST_IDS:
        await matcher.finish()
    session_id = event.get_session_id()
    user_id_str = str(event.user_id)
    user_text = event.get_plaintext().lstrip('#').strip()
    history = data_store.get_or_create_challenge_history(session_id)
    player_name = event.sender.card or event.sender.nickname or user_id_str
    shopkeeper_name = f"{player_name}的神秘店长"
    group_id_str = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    if user_text.lower() in ["rank", "排行榜", "leaderboard"]:
        if not group_id_str:
            await matcher.send("排行榜功能仅在群聊中可用哦。")
            return
        leaderboard = data_store.get_leaderboard(group_id_str)
        if not leaderboard:
            await matcher.send("本群还没有人成功攻略猫娘，快来成为第一人吧！")
            return
        rank_list = ["🏆 本群猫娘速通排行榜 🏆"]
        for i, record in enumerate(leaderboard):
            rank_list.append(f"第 {i+1} 名: {record.get('user_name', '未知玩家')} ({record.get('user_id', '未知ID')})\n所用字数: {record.get('char_count', 'N/A')}")
        await matcher.send("\n\n".join(rank_list))
        return
    if user_text.lower() in ["history", "历史"]:
        if not history:
            await matcher.send("你和猫娘们还没有任何对话记录哦，快去开启故事吧！")
            return
        history_text_parts = []
        for record in history:
            role, content = record.get("role"), record.get("content", "")
            if role == "user": history_text_parts.append(f"你：{content}")
            elif role == "assistant": history_text_parts.append(f"旁白/猫娘：\n{content}")
        full_history_text = "\n\n---\n\n".join(history_text_parts)
        await utils.send_long_message_as_forward(bot, event, full_history_text, f"{player_name}的游戏记录")
        return
    if isinstance(event, GroupMessageEvent):
        try: await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        except: pass
    is_reset_command = user_text.lower() in ["新游戏", "重置", "restart"]
    is_new_game = is_reset_command or not history
    messages_for_api = []
    if is_new_game:
        history.clear()
        data_store.reset_challenge_char_count(session_id)
        if is_reset_command: await matcher.send("...记忆已重置，咖啡馆的故事重新开始了。")
    else:
        data_store.increment_challenge_char_count(session_id, user_text)
        history.append({"role": "user", "content": user_text})
        messages_for_api = list(history)
    logger.info(f"会话 {session_id} (店长: {shopkeeper_name}) - 新游戏: {is_new_game} | 用户输入: '{user_text}'")
    try:
        api_response = await llm_client.call_gemini_api(messages=messages_for_api, system_prompt_content=config.CHALLENGE_SYSTEM_PROMPT, model_to_use=config.CHALLENGE_MODEL_NAME, use_tools=False)
        if "error" in api_response: raise RuntimeError(api_response.get("error", {}).get("message", "发生未知API错误"))
        full_response_content = api_response["choices"][0]["message"].get("content", "")
        game_state_jsons = re.findall(r"<GAME_STATE>(.*?)</GAME_STATE>", full_response_content, re.DOTALL)
        narrative_content = re.sub(r"<GAME_STATE>.*?</GAME_STATE>", "", full_response_content, flags=re.DOTALL).strip()
        feedback_messages, has_victory = [], False
        for json_str in game_state_jsons:
            try:
                game_data = json.loads(json_str)
                status, char = game_data.get("status"), game_data.get("character", "她")
                feedback = ""
                if status == "trust_up": feedback = f"（{char}对你的信赖似乎上升了。{game_data.get('reason', '')}）"
                elif status == "trust_down": feedback = f"（{char}对你的信赖似乎下降了。{game_data.get('reason', '')}）"
                elif status == "victory":
                    feedback = f"（🎉🎉🎉 恭喜！你与{char}的羁绊达成了！现在可以和她进行更深入的日常互动了~）"
                    has_victory = True
                if feedback: feedback_messages.append(feedback)
            except json.JSONDecodeError: logger.error(f"解析游戏状态JSON失败: {json_str}")
        feedback_block = "\n".join(feedback_messages)
        char_count_feedback = f"(本局游戏您已输入 {data_store.get_challenge_char_count(session_id)} 字)"
        final_content_parts = [p for p in [narrative_content, feedback_block, char_count_feedback] if p]
        final_content = "\n\n".join(final_content_parts).strip()
        if final_content:
            if narrative_content: history.append({"role": "assistant", "content": narrative_content})
            if len(final_content) > config.FORWARD_TRIGGER_THRESHOLD:
                await utils.send_long_message_as_forward(bot, event, final_content, shopkeeper_name)
            else:
                await matcher.send(Message(final_content))
        elif not is_new_game:
            await matcher.send("...她似乎没什么反应。")
        if has_victory and group_id_str and sum(1 for msg in history if "恭喜！你与" in msg.get("content", "")) == 0:
            data_store.update_leaderboard(group_id_str, user_id_str, player_name, data_store.get_challenge_char_count(session_id))
            await matcher.send(f"🎉恭喜 {player_name} 首次攻略成功！您的成绩已记录到本群速通排行榜！\n使用 `#排行榜` 查看。")
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
                await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10024')
            except: pass
    except Exception as e:
        if not is_new_game and history and history[-1]['role'] == 'user': history.pop()
        logger.error(f"处理猫娘咖啡馆时发生错误: {e}", exc_info=True)
        await matcher.send(f"...[叙事模块故障: {e}]...")
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
                await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
            except: pass


async def update_summary_for_group(group_id: str, history_list: list):
    # ...
    logger.info(f"正在为群组 {group_id} 生成摘要...")
    old_summary = data_store.get_group_summary(group_id)
    history_str = "\n".join(format_history_for_prompt(history_list))
    summary_prompt = f"""...""" # Prompt content is long, omitted for brevity
    try:
        api_response = await llm_client.call_gemini_api(messages=[{"role": "user", "content": summary_prompt}], system_prompt_content="", model_to_use=config.DEFAULT_MODEL_NAME, use_tools=False)
        new_summary = api_response["choices"][0]["message"].get("content", "").strip()
        if new_summary:
            data_store.update_group_summary(group_id, new_summary)
    except Exception as e:
        logger.error(f"为群组 {group_id} 生成摘要时出错: {e}")

def is_bilibili_card() -> Rule:
    # ...
    async def _checker(event: GroupMessageEvent) -> bool:
        if not isinstance(event, GroupMessageEvent): return False
        for seg in event.message:
            if seg.type == "json":
                try:
                    if json.loads(seg.data.get("data", "{}")).get("meta", {}).get("detail_1", {}).get("appid") == "1109937557": return True
                except: continue
        return False
    return Rule(_checker)

async def expand_b23_url(short_url: str) -> str:
    # ...
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.head(short_url, timeout=10.0)
            return urlunparse(urlparse(str(resp.url))._replace(params='', query='', fragment=''))
    except Exception as e:
        logger.error(f"展开或净化B站短链接 {short_url} 时出错: {e}")
        return short_url

bili_card_parser = on_message(rule=is_bilibili_card(), priority=20, block=False)
@bili_card_parser.handle()
async def handle_bili_card(bot: Bot, event: GroupMessageEvent, matcher: Matcher):
    # ...
    for seg in event.message:
        if seg.type == "json":
            try:
                short_url = json.loads(seg.data.get("data", "{}")).get("meta", {}).get("detail_1", {}).get("qqdocurl")
                if short_url:
                    long_url = await expand_b23_url(short_url)
                    await matcher.send(Message([MessageSegment.reply(id_=event.message_id), MessageSegment.text(long_url)]))
                    bot_name = (await bot.get_login_info())['nickname'] or "一猫"
                    data_store.get_group_history(str(event.group_id)).append({"timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": bot.self_id, "user_name": bot_name, "content": long_url, "is_bot": True})
                    return
            except Exception as e:
                logger.error(f"解析B站小程序时出错: {e}", exc_info=True)


# 【核心修改】群聊记录器
group_message_recorder = on_message(priority=1, block=False)
@group_message_recorder.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    if str(event.user_id) in config.USER_BLACKLIST_IDS: return
    
    group_id, user_id = str(event.group_id), str(event.user_id)
    history = data_store.get_group_history(group_id)
    
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(user_id))
        user_name = member_info.get('card') or member_info.get('nickname') or user_id
    except Exception:
        user_name = event.sender.nickname or user_id
    
    # 【关键】调用新的、能处理图片的 format_message_for_history
    structured_content = await format_message_for_history(bot, event)
    
    history.append({
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id, "user_name": user_name,
        "content": structured_content, "is_bot": user_id == bot.self_id
    })
    logger.debug(f"[记录员 V3] 已记录群({group_id})消息并预处理图片。")
    
    if user_id != bot.self_id:
        data_store.increment_active_chat_message_count(group_id)
        if data_store.increment_and_check_summary_trigger(group_id):
            asyncio.create_task(update_summary_for_group(group_id, list(history)))

# 【核心修改】此函数现在返回结构化内容，并能即时生成图片摘要
async def format_message_for_history(bot: Bot, event: GroupMessageEvent) -> Any:
    message = event.message
    has_non_text = any(seg.type != 'text' for seg in message)

    if not has_non_text: return event.get_plaintext()

    content_list, text_buffer = [], []
    if event.reply:
        try:
            replied_msg_info = await bot.get_msg(message_id=event.reply.message_id)
            replied_sender = replied_msg_info.get('sender', {})
            replied_user_name = replied_sender.get('card') or replied_sender.get('nickname', f"用户{replied_sender.get('user_id')}")
            raw_msg = replied_msg_info.get('message', '')
            if isinstance(raw_msg, dict): raw_msg = [raw_msg]
            replied_content = Message(raw_msg).extract_plain_text().strip() or "[非文本消息]"
            text_buffer.append(f"回复({replied_user_name}: “{replied_content[:20]}...”) ")
        except Exception as e:
            logger.warning(f"获取被回复消息({event.reply.message_id})失败: {e}")
            text_buffer.append("[回复了一条消息] ")

    for seg in message:
        if seg.type == 'text': text_buffer.append(seg.data.get('text', ''))
        else:
            if text_buffer:
                content_list.append({"type": "text", "text": "".join(text_buffer)})
                text_buffer = []
            if seg.type == 'image':
                img_url = seg.data.get('url')
                if img_url:
                    try:
                        async with httpx.AsyncClient() as c:
                            resp = await c.get(img_url, timeout=60.0)
                            resp.raise_for_status()
                            img_b64 = base64.b64encode(resp.content).decode()
                        # 【关键】为主动聊天图片摘要使用更快的模型
                        summary = await llm_client.summarize_image_content(img_b64, model_to_use=config.SLASH_COMMAND_MODEL_NAME)
                        content_list.append({"type": "image", "summary": summary})
                        logger.info(f"主动聊天记录：已为新图片生成摘要。")
                    except Exception as e:
                        logger.error(f"为主动聊天下载/摘要图片时失败: {img_url}, error: {e}")
                        content_list.append({"type": "text", "text": "[图片处理失败]"})
                else: content_list.append({"type": "text", "text": "[图片]"})
            elif seg.type != 'reply': content_list.append({"type": "text", "text": f"[{seg.type}]"})
    
    if text_buffer: content_list.append({"type": "text", "text": "".join(text_buffer)})
    return content_list


def format_history_for_prompt(hist_list: List[Dict]) -> List[str]:
    # ... (此函数保持不变) ...
    formatted_lines = []
    for msg in hist_list:
        user_info = f"[{msg['timestamp']}] [用户ID:{msg['user_id']} (昵称:{msg['user_name']})]:"
        content, content_str = msg.get('content'), ""
        if isinstance(content, str): content_str = content
        elif isinstance(content, list):
            parts = []
            for item in content:
                if item.get("type") == "text": parts.append(item.get("text", ""))
                elif item.get("type") == "image" and "summary" in item: parts.append(f"[图片: {item['summary']}]")
                else: parts.append(f"[{item.get('type', '未知内容')}]")
            content_str = " ".join(parts)
        formatted_lines.append(f"{user_info} {content_str.strip()}")
    return formatted_lines


async def handle_active_chat_check(bot: Bot, event: GroupMessageEvent):
    # ... (此函数保持不变) ...
    group_id = str(event.group_id)
    if not config.ACTIVE_CHAT_ENABLED or group_id not in config.ACTIVE_CHAT_WHITELIST or not data_store.check_and_set_cooldown(group_id): return
    if data_store.get_active_chat_message_count(group_id) < config.ACTIVE_CHAT_MESSAGE_THRESHOLD: return
    history = data_store.get_group_history(group_id)
    if not history: return
    group_summary = data_store.get_group_summary(group_id)
    history_for_prompt = format_history_for_prompt(list(history))
    if not history_for_prompt: return
    recent_history, new_message = "\n".join(history_for_prompt[:-1]), history_for_prompt[-1]
    decision_payload = f"Group Summary:\n{group_summary}\n\nRecent History:\n{recent_history}\n\nNew Message:\n{new_message}"
    decision_messages = [{"role": "user", "content": decision_payload}]
    system_prompt = config.ACTIVE_CHAT_DECISION_PROMPT.format(current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        logger.info(f"[主动聊天] 群({group_id}) 正在进行决策 (上下文包含图片摘要)...")
        api_response = await llm_client.call_gemini_api(messages=decision_messages, system_prompt_content=system_prompt, model_to_use=config.ACTIVE_CHAT_DECISION_MODEL, use_tools=False)
        if "error" in api_response:
            logger.error(f"[主动聊天] 决策API调用失败: {api_response['error']}")
            return
        response_content = api_response["choices"][0]["message"].get("content", "").strip("```json").strip("```").strip()
        decision_data = json.loads(response_content)
        if decision_data.get("should_reply"):
            reply_text = decision_data.get("reply_content", "").strip()
            if reply_text:
                logger.info(f"[主动聊天] 决定回复群({group_id})，内容: {reply_text}")
                await bot.send(event, message=reply_text)
                data_store.reset_active_chat_message_count(group_id)
                bot_name = (await bot.get_login_info())['nickname'] or "一猫"
                history.append({"timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": bot.self_id, "user_name": bot_name, "content": reply_text, "is_bot": True})
    except json.JSONDecodeError:
        logger.warning(f"[主动聊天] 解析决策JSON失败: {response_content}")
    except Exception as e:
        logger.error(f"[主动聊天] 处理过程中发生未知错误: {e}", exc_info=True)