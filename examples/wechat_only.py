"""微信收发示例，无 Codex、HTTP 服务或任务数据库。默认只读，--send 才发送。"""
import argparse
import dataclasses
import json
import time

from wxagentbot import ChatTarget, WechatClient, WechatOptions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", nargs=2, metavar=("CHAT_ID", "TEXT"))
    args = parser.parse_args()
    chats = [ChatTarget("yeiban", "夜伴"), ChatTarget("aaa-group", "AAA猛攻建材批发", "group")]
    if args.send:
        chats = [c for c in chats if c.id == args.send[0]]
        if not chats:
            parser.error("未知会话 ID")
    client = WechatClient(WechatOptions(listen_mode="independent"))
    try:
        client.prepare_windows(chats)  # 一次性打开窗口，需要桌面可操作。
        if args.send:
            client.send(chats[0], args.send[1])  # 原样发送，不自动修改 Markdown 或正文。
            return
        snapshots = {}
        while True:
            client.pump()
            for chat in chats:
                messages = client.read(chat)
                fingerprint = tuple(m.fingerprint() for m in messages)
                if snapshots.get(chat.id) != fingerprint:
                    snapshots[chat.id] = fingerprint
                    print(json.dumps({"chat": chat.id, "snapshot": [dataclasses.asdict(m) for m in messages]},
                                     ensure_ascii=False), flush=True)
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
