# yimao_plugin/utils.py
import logging
from . import config, data_store # 导入 data_store
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent

logger = logging.getLogger("GeminiPlugin.utils")

def get_help_menu() -> str:
    """生成并返回帮助菜单的字符串"""
    return (
        "主要功能：\n"
        "1.双轨对话系统\n"
        "描述: 机器人为每个用户维护两套独立的对话记忆系统，【普通模式】和【Loki模式】。每套系统内部都有10个可切换的记忆插槽，且所有记忆都会被永久保存。\n\n"
        "  a. 普通对话 (一猫)\n"
        "  触发方式: @一猫 [问题]\n"
        "  描述: 用于日常问答和工具调用。\n\n"
        "  b. Loki对话\n"
        "  触发方式: @一猫 /[问题]\n"
        "  描述: Loki人格，用于角色扮演和创意写作，通常生成数千字的内容且对可能的违规内容不做限制（已解限）。如果想得到中文回复，需要在问题里让他说中文。\n\n"
        "2.图片理解\n"
        "触发方式: 回复一张图片，并在回复内容中 @一猫 [你对图片的问题]\n"
        "描述: 可以理解并分析您回复的图片内容，并能在分析后调用工具（如查询天气）。\n\n"
        "3.记忆管理\n"
        "描述: 分别管理两套系统的记忆插槽。\n"
        "查看列表: /memory (普通模式)  或  //memory (Loki模式)\n"
        "切换插槽: /memory [编号]  或  //memory [编号]\n"
        "清空当前: /restart        或  //restart\n\n"
        "4.禁漫下载\n"
        "触发方式: /jm [禁漫号]\n"
        "描述: 下载指定禁漫号的本子为PDF格式并发送。例如：/jm 123456\n\n"
        "随机本子: /随机jm\n\n"
        "5.猜病挑战\n"
        "触发方式: #[你的话]\n"
        "描述: 与一个特定“病人”对话，通过提问诊断出他/她的病症。使用 `#新游戏` 可重置挑战。\n\n"
        "6.帮助\n"
        "触发方式: @一猫 help\n"
        "描述: 显示此帮助菜单。"
    )

async def send_long_message_as_forward(bot: Bot, event: Event, content: str, bot_name: str):
    """将长文本按指定大小分割后，作为合并转发消息发送，并缓存其内容。"""
    
    chunk_size = config.FORWARD_NODE_CHUNK_SIZE
    content_chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    
    forward_nodes = [
        {"type": "node", "data": {"uin": bot.self_id, "name": bot_name, "content": chunk}}
        for chunk in content_chunks
    ]
    
    try:
        sent_receipt = None
        if isinstance(event, GroupMessageEvent):
            sent_receipt = await bot.send_group_forward_msg(group_id=event.group_id, messages=forward_nodes)
        else:
            sent_receipt = await bot.send_private_forward_msg(user_id=event.user_id, messages=forward_nodes)

        # 【核心修正】如果发送成功并拿到了消息ID，立刻缓存原始内容
        if sent_receipt and 'message_id' in sent_receipt:
            data_store.cache_forward_content(int(sent_receipt['message_id']), content)

        return sent_receipt
        
    except Exception as e:
        logger.error(f"发送合并转发消息失败: {e}", exc_info=True)
        await bot.send(event, message="喵呜~ 我想说的内容太多，被QQ拦截了，没办法发出来...")
        return None