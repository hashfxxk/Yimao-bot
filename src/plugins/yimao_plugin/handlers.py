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
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from typing import Literal

from jmcomic import create_option_by_file, download_album, JmcomicClient
from jmcomic.jm_exception import MissingAlbumPhotoException, PartialDownloadFailedException

from nonebot import on_message
from nonebot.rule import Rule
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Bot, Event, Message, GroupMessageEvent, MessageSegment

from . import config, data_store, llm_client, tools, utils

logger = logging.getLogger("GeminiPlugin.handlers")

DownloadResult = Literal["ok", "not_found", "error"]


async def run_jm_download_task(bot: Bot, event: Event, album_id: str) -> DownloadResult:
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

async def handle_chat_session(bot: Bot, matcher: Matcher, event: Event, user_message_payload: dict):
    session_id = event.get_session_id()
    if isinstance(event, GroupMessageEvent):
        try:
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        except: pass
    
    # 【最终修正】不再有摘要，content就是一切
    content = user_message_payload["content"]

    prompt_text = ""
    if isinstance(content, str):
        prompt_text = content
    elif isinstance(content, list):
        for item in content:
            if item.get('type') == 'text':
                prompt_text = item['text']
                break

    is_slash_mode = prompt_text.lstrip().startswith('/')
    mode = "slash" if is_slash_mode else "normal"

    data_store.update_slot_summary_if_needed(session_id, mode, prompt_text)
    
    history = data_store.get_active_history(session_id, mode)
    
    now_ts_str = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    
    # 【最终修正】构造忠实、完整的历史记录
    history_record_for_user = {"role": "user", "message_id": event.message_id}
    
    if isinstance(content, str):
         history_record_for_user['content'] = content if is_slash_mode else now_ts_str + content
    else: # is list
        history_content_copy = [dict(item) for item in content]
        for item in history_content_copy:
            if item.get('type') == 'text':
                item['text'] = item['text'] if is_slash_mode else now_ts_str + item['text']
                break
        history_record_for_user['content'] = history_content_copy
        
    history.append(history_record_for_user)
    
    messages_for_api = list(history)

    if mode == "slash":
        model, system_prompt, use_function_calling = config.SLASH_COMMAND_MODEL_NAME, "", False
        if len(history) == 1:
            first_turn_payload = messages_for_api[0]
            original_content = ""
            if isinstance(first_turn_payload.get('content'), str):
                original_content = first_turn_payload['content'].lstrip(now_ts_str)
                first_turn_payload['content'] = f"{config.SLASH_COMMAND_SYSTEM_PROMPT}\n\n---\n\n{original_content.lstrip('/')}"
            elif isinstance(first_turn_payload.get('content'), list):
                 for item in first_turn_payload['content']:
                     if item.get('type') == 'text':
                         original_content = item['text'].lstrip(now_ts_str)
                         item['text'] = f"{config.SLASH_COMMAND_SYSTEM_PROMPT}\n\n---\n\n{original_content.lstrip('/')}"
                         break
    else: 
        model, system_prompt, use_function_calling = config.DEFAULT_MODEL_NAME, config.DEFAULT_SYSTEM_PROMPT_TEMPLATE, True
        
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
                logger.info("模型请求调用工具...")
                assistant_message = {"role": "assistant", "tool_calls": response_message["tool_calls"]}
                if response_message.get("content"):
                    assistant_message["content"] = response_message["content"]
                
                messages_for_api.append(assistant_message)
                history.append(assistant_message) 

                for tool_call in response_message["tool_calls"]:
                    function_name = tool_call["function"]["name"]
                    function_args = json.loads(tool_call["function"].get("arguments", "{}"))
                    if function_name in tools.available_tools:
                        function_to_call = tools.available_tools[function_name]
                        tool_output = await function_to_call(**function_args) if asyncio.iscoroutinefunction(function_to_call) else function_to_call(**function_args)
                        tool_response = {"tool_call_id": tool_call["id"], "role": "tool", "name": function_name, "content": tool_output}
                        messages_for_api.append(tool_response)
                        history.append(tool_response)
                    else:
                        tool_error_response = {"tool_call_id": tool_call["id"], "role": "tool", "name": function_name, "content": f"错误: 函数 '{function_name}' 未定义。"}
                        messages_for_api.append(tool_error_response)
                        history.append(tool_error_response)
                continue
            else:
                response_content = response_message.get("content", "")
                if isinstance(event, GroupMessageEvent) and response_content:
                    history_for_active_chat = data_store.get_group_history(str(event.group_id))
                    bot_name = "Loki" if mode == "slash" else (await bot.get_login_info())['nickname'] or "一猫"
                    # 这里我们只记录干净的、最终的回复内容
                    structured_message = {
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "user_id": bot.self_id,
                        "user_name": bot_name,
                        "content": response_content,
                        "is_bot": True
                    }
                    history_for_active_chat.append(structured_message)
                    logger.debug(f"[回写] 已记录机器人聊天回复到群({event.group_id})历史。")
                assistant_timestamped_content = response_content if is_slash_mode else datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + response_content
                assistant_message_payload = {"role": "assistant", "content": assistant_timestamped_content}
                sent_msg_receipt = None

                if len(response_content) > config.FORWARD_TRIGGER_THRESHOLD:
                    bot_name = "Loki" if mode == "slash" else "一猫"
                    sent_msg_receipt = await utils.send_long_message_as_forward(bot, event, response_content, bot_name)
                elif response_content:
                    sent_msg_receipt = await matcher.send(Message(response_content))
                else:
                    logger.warning(f"从API收到了空的响应内容: {api_response}")
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


async def handle_memory_command(matcher: Matcher, event: Event, args: Message = CommandArg()):
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

async def handle_clear_command(matcher: Matcher, event: Event):
    session_id = event.get_session_id()
    command = event.get_plaintext().strip()
    mode = "slash" if command.startswith('//') else "normal"
    result_message = data_store.clear_active_slot(session_id, mode)
    await matcher.send(result_message)
    data_store.save_memory_to_file()





async def handle_challenge_chat(bot: Bot, matcher: Matcher, event: Event):
    session_id = event.get_session_id()
    user_text = event.get_plaintext().lstrip('#').strip()
    history = data_store.get_or_create_challenge_history(session_id)
    is_new_game = not history
    if user_text.lower() in ["新游戏", "重置", "restart"]:
        data_store.clear_challenge_history(session_id)
        is_new_game = True
        history = data_store.get_or_create_challenge_history(session_id)
    elif not user_text and not is_new_game:
        await matcher.finish("医生，请输入你的诊断问题。")
        return
    if not is_new_game:
        user_message_payload = {"role": "user", "content": user_text}
        history.append(user_message_payload)
    logger.info(f"会话 {session_id} (猜病挑战) 收到请求 (新游戏: {is_new_game}): '{user_text}'")
    try:
        api_response = await llm_client.call_gemini_api(
            messages=list(history),
            system_prompt_content=config.CHALLENGE_SYSTEM_PROMPT,
            model_to_use=config.CHALLENGE_MODEL_NAME,
            use_tools=False
        )
        if "error" in api_response:
            error_data = api_response.get("error", {})
            error_msg_from_api = error_data.get("message", "发生未知错误")
            logger.error(f"猜病挑战API调用失败: {error_msg_from_api}")
            await matcher.send(f"诊断设备出错了: {error_msg_from_api}")
            if history and history[-1]['role'] == 'user':
                history.pop()
            return
        response_content = api_response["choices"][0]["message"].get("content", "")
        if response_content:
             history.append({"role": "assistant", "content": response_content})
             await matcher.send(Message(response_content))
             if "不是神经的问题吗" in response_content or "我自己的问题" in response_content:
                data_store.clear_challenge_history(session_id)
                await matcher.send("（游戏已结束，可使用 `#新游戏` 重新开始）")
        else:
            logger.warning(f"从猜病挑战API收到了空的响应内容: {api_response}")
            await matcher.send("...信号中断...")
    except httpx.HTTPStatusError as e:
        logger.error(f"处理猜病挑战时发生HTTP错误: {e.response.status_code} - {e.response.text}", exc_info=True)
        await matcher.send(f"...[严重错误：与核心逻辑单元的连接失败，状态码: {e.response.status_code}]...")
    except Exception as e:
        if history and not is_new_game and history[-1]['role'] == 'user':
            history.pop()
        logger.error(f"处理猜病挑战时发生未知错误: {e}", exc_info=True)
        await matcher.send("...[严重错误：诊断模块发生未知故障]...")

async def update_summary_for_group(group_id: str, history_list: list):
    logger.info(f"正在为群组 {group_id} 生成摘要...")
    old_summary = data_store.get_group_summary(group_id)
    
    # 将结构化的历史记录转换成对AI友好的字符串格式
    def format_history_for_summary(hist_list):
        formatted = []
        for msg in hist_list:
            if isinstance(msg, dict): # 兼容新格式
                formatted.append(f"{msg['timestamp']} [用户ID:{msg['user_id']} (昵称:{msg['user_name']})]: {msg['content']}")
            else: # 兼容可能存在的旧格式字符串
                formatted.append(str(msg))
        return "\n".join(formatted)

    history_str = format_history_for_summary(history_list)
    
    summary_prompt = f"""
    你是一个社群观察家，你的任务是阅读一段群聊记录和旧的群聊摘要，然后生成一个新的、更完善的摘要。
    【旧摘要】
    {old_summary}
    【近期聊天记录】
    {history_str}
    【你的任务】
    请根据以上信息，提炼并更新群聊摘要。摘要应包含：
    1.  群聊的核心主题或氛围。
    2.  识别出几位最活跃的群友及其典型特征（请使用他们的昵称，但要基于用户ID来区分不同的人）。
    3.  记录一些群内最近发生的、可能会在未来被再次提到的大事或流行的梗。
    请以简洁、客观的语言输出新的摘要。
    """
    try:
        api_response = await llm_client.call_gemini_api(
            messages=[{"role": "user", "content": summary_prompt}],
            system_prompt_content="",
            model_to_use=config.DEFAULT_MODEL_NAME,
            use_tools=False
        )
        new_summary = api_response["choices"][0]["message"].get("content", "").strip()
        if new_summary:
            data_store.update_group_summary(group_id, new_summary)
    except Exception as e:
        logger.error(f"为群组 {group_id} 生成摘要时出错: {e}")
    


def is_bilibili_card() -> Rule:
    """
    它会检查消息段中是否包含json类型，并且json内容中包含B站小程序的固定AppID '1109937557'。
    """
    async def _checker(event: GroupMessageEvent) -> bool:
        if not isinstance(event, GroupMessageEvent):
            return False
        
        for seg in event.message:
            if seg.type == "json":
                try:
                    json_data = json.loads(seg.data.get("data", "{}"))
                    
                    if json_data.get("meta", {}).get("detail_1", {}).get("appid") == "1109937557":
                        return True
                except (json.JSONDecodeError, AttributeError):
                    continue
        return False
    return Rule(_checker)

async def expand_b23_url(short_url: str) -> str:
    """
    访问短链接，返回长链接。
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            head_response = await client.head(short_url, timeout=10.0)
            long_url_with_params = str(head_response.url)
            
            parsed_url = urlparse(long_url_with_params)
            
            clean_url = urlunparse(parsed_url._replace(params='', query='', fragment=''))
            
            return clean_url
            
    except Exception as e:
        logger.error(f"展开或净化B站短链接 {short_url} 时出错: {e}")
        return short_url

bili_card_parser = on_message(rule=is_bilibili_card(), priority=20, block=False)

@bili_card_parser.handle()
async def handle_bili_card(bot: Bot, event: GroupMessageEvent, matcher: Matcher):
    for seg in event.message:
        if seg.type == "json":
            try:
                json_data = json.loads(seg.data.get("data", "{}"))
                short_url = json_data.get("meta", {}).get("detail_1", {}).get("qqdocurl")
                
                if short_url:
                    long_url = await expand_b23_url(short_url)
                    
                    reply_text = long_url
                    
                    reply_segment = MessageSegment.reply(id_=event.message_id)
                    text_segment = MessageSegment.text(reply_text)
                    message_to_send = Message([reply_segment, text_segment])
                    
                    logger.info(f"成功解析并展开B站链接: {short_url} -> {long_url}")
                    await matcher.send(message_to_send)
                    
                    # 【核心修改】在这里把机器人的发言写回历史记录
                    history = data_store.get_group_history(str(event.group_id))
                    bot_name = (await bot.get_login_info())['nickname'] or "一猫"
                    structured_message = {
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "user_id": bot.self_id,
                        "user_name": bot_name,
                        "content": reply_text,
                        "is_bot": True
                    }
                    history.append(structured_message)
                    logger.debug(f"[回写] 已记录B站解析回复到群({event.group_id})历史。")
                    
                    return

            except Exception as e:
                logger.error(f"解析B站小程序时出错: {e}", exc_info=True)

group_message_recorder = on_message(priority=1, block=False)

@group_message_recorder.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    """
    这个处理器拥有最高优先级，像一个忠实的书记官，
    在任何功能被触发之前，就将所有群聊消息记录到历史中。
    """
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    history = data_store.get_group_history(group_id)
    
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(user_id))
        user_name = member_info.get('card') or member_info.get('nickname') or user_id
    except Exception:
        user_name = event.sender.nickname or user_id
        
    message_text = event.get_plaintext().strip()
    if not message_text:
        message_text = await _describe_message_content_for_active_chat(bot, event.message)
    
    structured_message = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "user_name": user_name,
        "content": message_text,
        "is_bot": user_id == bot.self_id
    }
    history.append(structured_message)
    logger.debug(f"[记录员 V_Final] 已记录群({group_id})消息: {user_name}: {message_text[:30]}...")
    
    if user_id != bot.self_id and data_store.increment_and_check_summary_trigger(group_id):
        asyncio.create_task(update_summary_for_group(group_id, list(history)))

async def _describe_message_content_for_active_chat(bot: Bot, message: Message) -> str:
    """为主动聊天历史记录，简单描述非纯文本消息。"""
    if not message: return "[一条空消息]"
    for seg in message:
        if seg.type == 'image': return "[图片]"
        if seg.type == 'face': return "[表情]"
        if seg.type == 'record': return "[语音]"
        if seg.type == 'json': return "[小程序/卡片]"
    return "[一条非纯文本消息]"

# 2. 主动聊天决策者 (注意：这个就是你 `__init__.py` 文件末尾的 `active_chat_handler` 所调用的函数)
# 我们把它放在这里，但让 `__init__.py` 来调用
async def handle_active_chat_check(bot: Bot, event: GroupMessageEvent):
    """这个处理器只在所有其他处理器都运行完毕后，才根据完整的历史记录进行决策。"""
    if not config.ACTIVE_CHAT_ENABLED or str(event.group_id) not in config.ACTIVE_CHAT_WHITELIST:
        return
    if not data_store.check_and_set_cooldown(str(event.group_id)):
        return

    group_id = str(event.group_id)
    history = data_store.get_group_history(group_id)
    if not history: return

    group_summary = data_store.get_group_summary(group_id)

    def format_history_for_prompt(hist_list):
        formatted = []
        for msg in hist_list:
            if isinstance(msg, dict):
                formatted.append(f"[{msg['timestamp']}] [用户ID:{msg['user_id']} (昵称:{msg['user_name']})]: {msg['content']}")
            elif isinstance(msg, str):
                formatted.append(msg)
        return formatted

    history_for_prompt = format_history_for_prompt(list(history))
    if not history_for_prompt: return
    
    recent_history_prompt = history_for_prompt[:-1]
    new_message_prompt = history_for_prompt[-1]

    decision_payload = {
        "group_summary": group_summary,
        "recent_history": recent_history_prompt,
        "new_message": new_message_prompt
    }
    
    decision_messages = [{"role": "user", "content": json.dumps(decision_payload, ensure_ascii=False)}]
    system_prompt_with_time = config.ACTIVE_CHAT_DECISION_PROMPT.format(current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    try:
        logger.info(f"[主动聊天] 群({group_id}) 正在进行决策...")
        api_response = await llm_client.call_gemini_api(
            messages=decision_messages,
            system_prompt_content=system_prompt_with_time,
            model_to_use=config.ACTIVE_CHAT_DECISION_MODEL,
            use_tools=False
        )
        if "error" in api_response:
            logger.error(f"[主动聊天] 决策API调用失败: {api_response['error']}")
            return

        response_content = api_response["choices"][0]["message"].get("content", "")
        if response_content.startswith("```json"):
            response_content = response_content.strip("```json").strip("```").strip()
        
        decision_data = json.loads(response_content)
        
        if decision_data.get("should_reply") is True:
            reply_text = decision_data.get("reply_content", "").strip()
            if reply_text:
                logger.info(f"[主动聊天] 决定回复群({group_id})，内容: {reply_text}")
                await bot.send(event, message=reply_text)
                
                bot_name = (await bot.get_login_info())['nickname'] or "一猫"
                structured_message = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "user_id": bot.self_id,
                    "user_name": bot_name,
                    "content": reply_text,
                    "is_bot": True
                }
                history.append(structured_message)
                logger.debug(f"[回写] 已记录主动聊天回复到群({group_id})历史。")
    except json.JSONDecodeError:
        logger.warning(f"[主动聊天] 解析决策JSON失败: {response_content}")
    except Exception as e:
        logger.error(f"[主动聊天] 处理过程中发生未知错误: {e}", exc_info=True)