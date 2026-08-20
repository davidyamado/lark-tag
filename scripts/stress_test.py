#!/usr/bin/env python3
"""
压测脚本：并发调用 stream_chat，测量吞吐量和延迟。
用法：python scripts/stress_test.py --concurrency 3 --total 9 --prompt "你好"
"""
import argparse
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent import Agent, StreamResult
from src.config import Config


def single_request(agent: Agent, user_idx: int, prompt: str) -> dict:
    open_id = f"stress_test_user_{user_idx % 5}"  # 5 虚拟用户轮换
    t0 = time.monotonic()
    result = None
    chunks = 0
    try:
        for chunk in agent.stream_chat(
            open_id=open_id,
            text=prompt,
            session_id=None,
            max_turns=3,
        ):
            if isinstance(chunk, StreamResult):
                result = chunk
            elif isinstance(chunk, str):
                chunks += 1
    except Exception as e:
        return {"idx": user_idx, "ok": False, "error": str(e), "duration": time.monotonic() - t0}

    duration = time.monotonic() - t0
    return {
        "idx": user_idx,
        "ok": result and not result.is_error,
        "error": None,
        "duration": duration,
        "cost": result.cost_usd if result else 0,
        "chunks": chunks,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=3, help="并发数")
    parser.add_argument("--total", type=int, default=9, help="总请求数")
    parser.add_argument("--prompt", default="用一句话介绍你自己", help="测试 prompt")
    args = parser.parse_args()

    cfg = Config.from_env()
    agent = Agent(
        bot_home=cfg.lark_bot_home,
        users_dir=cfg.lark_users_dir,
        model=cfg.claude_model,
    )

    print(f"压测开始：concurrency={args.concurrency} total={args.total} prompt={args.prompt!r}")
    print("-" * 60)

    results = []
    lock = threading.Lock()
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(single_request, agent, i, args.prompt): i for i in range(args.total)}
        for fut in as_completed(futures):
            r = fut.result()
            with lock:
                results.append(r)
            status = "✓" if r["ok"] else "✗"
            print(f"  [{status}] #{r['idx']:02d} {r['duration']:.1f}s  cost=${r.get('cost', 0):.4f}  {r.get('error') or ''}")

    total_time = time.monotonic() - t_start
    ok = [r for r in results if r["ok"]]
    fail = [r for r in results if not r["ok"]]
    durations = [r["duration"] for r in ok]

    print("-" * 60)
    print(f"总耗时:    {total_time:.1f}s")
    print(f"成功/失败: {len(ok)}/{len(fail)}")
    if durations:
        print(f"延迟 avg:  {sum(durations)/len(durations):.1f}s")
        print(f"延迟 min:  {min(durations):.1f}s")
        print(f"延迟 max:  {max(durations):.1f}s")
        print(f"总 cost:   ${sum(r.get('cost',0) for r in results):.4f}")
    if fail:
        print("\n失败详情:")
        for r in fail:
            print(f"  #{r['idx']:02d}: {r['error']}")


if __name__ == "__main__":
    main()
