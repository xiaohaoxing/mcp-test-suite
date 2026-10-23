"""命令行入口。

    python -m mcp_suite run      --suite <yaml> [--base-url] [--layer] [--priority] [--json] [--md]
    python -m mcp_suite validate --suite <yaml>
    python -m mcp_suite doc      --suite <yaml> [--out <md>]

退出码是契约，CI 与 Makefile 都按它分支：

    0  全过（todo / skip 不算失败）
    1  有失败（含单条用例的执行错误）
    2  配置错误（YAML 结构 / 令牌缺失 / 过滤后没有用例 / 文件读不到）

「配置错误」与「有失败」必须分开：前者说明**报告本身不可信**（比如某条用例的断言键
拼错了，它会被静默跳过），后者才是被测服务的问题。混成一个退出码，
夜间任务里就分不清「服务坏了」还是「套件坏了」。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Iterable, List, Optional, Sequence

from . import __version__
from .emit_doc import write_doc
from .loader import (
    LAYER_NOTES,
    PRIORITIES,
    Suite,
    SuiteConfigError,
    env_consumer_names,
    load_env_file,
    load_file,
    mask_token,
    parse_env_value,
)
from .report import build_report, render_console, render_json, render_markdown
from .runner import Runner


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "doc":
        return _cmd_doc(args)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_suite",
        description="MCP 能力验收套件：按 mcp-test-suite.yaml 跑 tool / http 两类动作并出报告",
    )
    parser.add_argument("--version", action="version", version=f"mcp_suite {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行套件")
    run.add_argument("--suite", required=True, help="套件 YAML 路径")
    run.add_argument("--base-url", help="覆盖 YAML 里的 defaults.base_url")
    run.add_argument(
        "--layer",
        action="append",
        default=[],
        help="只跑这些层（可重复，也可逗号分隔：--layer auth,guard）",
    )
    run.add_argument(
        "--priority",
        action="append",
        default=[],
        help="只跑这些优先级（可重复，也可逗号分隔：--priority P0）",
    )
    run.add_argument("--json", dest="json_out", help="JSON 报告输出路径")
    run.add_argument("--md", dest="md_out", help="Markdown 报告输出路径")
    run.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="从 dotenv 文件读令牌（可重复；**不覆盖**已存在的环境变量）",
    )
    run.add_argument(
        "--consumer",
        help="环境变量是 `token:消费者名` 列表时，按消费者名挑令牌",
    )
    run.add_argument("--token-env", help="覆盖 YAML 里的 token_env（环境变量名）")
    run.add_argument("--admin-token-env", help="覆盖 YAML 里的 admin_token_env（环境变量名）")
    run.add_argument("--timeout", type=float, help="覆盖 YAML 里的 defaults.timeout_s（秒）")
    run.add_argument(
        "--retry-429",
        type=int,
        default=5,
        help="被服务限流（429）时按 Retry-After 重试的次数（默认 5；断言 429 的用例不重试）",
    )
    run.add_argument(
        "--pace-ms",
        type=float,
        default=0.0,
        help="两次 HTTP 请求之间的最小间隔（毫秒，默认 0：靠 429 重试兜底）",
    )
    run.add_argument("--quiet", action="store_true", help="只打结论，不逐条打进度")
    run.add_argument("--no-pass", action="store_true", help="控制台不打印通过的用例")

    val = sub.add_parser("validate", help="只校验 YAML（不执行）")
    val.add_argument("--suite", required=True, help="套件 YAML 路径")

    doc = sub.add_parser("doc", help="把 YAML 渲染成 MCP-TEST-SUITE.md")
    doc.add_argument("--suite", required=True, help="套件 YAML 路径")
    doc.add_argument("--out", help="输出路径（默认与 YAML 同目录）")
    return parser


# --------------------------------------------------------------------------- run
def _cmd_run(args: argparse.Namespace) -> int:
    try:
        suite = load_file(args.suite)
        for env_file in args.env_file:
            load_env_file(env_file)
    except SuiteConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    layers = _split_multi(args.layer)
    priorities = _split_multi(args.priority)

    for layer in layers:
        if layer not in suite.layers:
            print(
                f"配置错误：--layer {layer!r} 不在套件声明的层里；已声明：{', '.join(suite.layers)}",
                file=sys.stderr,
            )
            return 2
    for pri in priorities:
        if pri not in PRIORITIES:
            print(f"配置错误：--priority 只能是 {'/'.join(PRIORITIES)}，实际 {pri!r}", file=sys.stderr)
            return 2

    cases = suite.select(layers, priorities)
    if not cases:
        print("配置错误：过滤后没有用例可跑。", file=sys.stderr)
        return 2

    token_env = args.token_env or suite.defaults.token_env
    admin_env = args.admin_token_env or suite.defaults.admin_token_env
    raw_token = os.environ.get(token_env)
    token, consumer = parse_env_value(raw_token, args.consumer)
    if raw_token and args.consumer and token is None:
        names = ", ".join(n for n in env_consumer_names(raw_token) if n) or "（无）"
        print(
            f"配置错误：{token_env} 里没有消费者 {args.consumer!r}；现有消费者：{names}",
            file=sys.stderr,
        )
        return 2
    admin_token = os.environ.get(admin_env) if admin_env else None

    # 令牌缺失是「配置错误」而不是「失败」：缺令牌时所有 tool 用例都会变成假红，
    # 那份报告不能用来判断服务好坏，必须先补环境再跑。
    need_business = any(
        c.runnable
        and (c.action.kind == "tool" or c.action.resolved_auth() == "business")
        for c in cases
    )
    need_admin = any(c.runnable and c.action.kind == "http" and c.action.resolved_auth() == "admin" for c in cases)
    if need_business and not token:
        print(
            f"配置错误：环境变量 {token_env} 未设置或为空，但有 tool 用例要跑。"
            f"请 export {token_env}=<令牌> 或用 --env-file 指定。",
            file=sys.stderr,
        )
        return 2
    if need_admin and not admin_token:
        print(
            f"配置错误：环境变量 {admin_env or '(未声明 admin_token_env)'} 未设置或为空，"
            f"但有管理面用例要跑。请 export 或在套件的 defaults.admin_token_env 里声明变量名。",
            file=sys.stderr,
        )
        return 2

    base_url = (args.base_url or suite.defaults.base_url).rstrip("/")
    timeout_s = args.timeout or suite.defaults.timeout_s
    runner = Runner(
        suite,
        base_url=base_url,
        token=token,
        admin_token=admin_token,
        consumer=consumer,
        timeout_s=timeout_s,
        retry_429=args.retry_429,
        pace_ms=args.pace_ms,
    )

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    t0 = time.monotonic()

    def progress(result) -> None:
        if args.quiet:
            return
        if result.status == "pass" and args.no_pass:
            return
        mark = {"pass": "PASS", "fail": "FAIL", "todo": "TODO", "skip": "SKIP"}[result.status]
        print(
            f"[{mark}] {result.case.id:<22} {result.case.layer}/{result.case.priority:<2} "
            f"{result.elapsed_ms:>6}ms  {result.case.title}",
            flush=True,
        )
        if result.status == "fail":
            if result.error:
                print(f"       执行错误：{result.error}", flush=True)
            for a in result.assertions:
                if a.ok:
                    continue
                print(
                    f"       {a.key}: 期望 {_short(a.expected)} / 实际 {_short(a.actual)}",
                    flush=True,
                )

    if not args.quiet:
        print(f"套件 {suite.suite} v{suite.version} · 目标 {base_url} · 令牌 {mask_token(token)}")
        print(f"用例 {len(cases)} 条（todo/skip 不执行）", flush=True)
        print("", flush=True)

    results = runner.run(cases, progress=progress)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    report = build_report(
        suite,
        results,
        base_url=base_url,
        token_masked=mask_token(token),
        consumer=consumer,
        timeout_s=timeout_s,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        layers=layers,
        priorities=priorities,
    )

    if args.json_out:
        _write(args.json_out, render_json(report))
    if args.md_out:
        _write(args.md_out, render_markdown(report))

    print("")
    print(render_console(report, show_pass=False, show_cases=False))

    failed = report["summary"]["fail"]
    if args.json_out:
        print(f"\nJSON 报告：{args.json_out}")
    if args.md_out:
        print(f"Markdown 报告：{args.md_out}")
    return 1 if failed else 0


# --------------------------------------------------------------------------- validate
def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        suite = load_file(args.suite)
    except SuiteConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"OK · {suite.suite} v{suite.version} · 服务 {suite.service} · {len(suite.cases)} 条用例")
    for layer in suite.layers:
        n = sum(1 for c in suite.cases if c.layer == layer)
        print(f"  {layer:<11} {n:>3} 条  {LAYER_NOTES.get(layer, '')}")
    for p in PRIORITIES:
        print(f"  {p}: {sum(1 for c in suite.cases if c.priority == p)} 条")
    todos = [c.id for c in suite.cases if c.status == "todo"]
    if todos:
        print(f"  todo: {len(todos)} 条（{', '.join(todos)}）")
    return 0


# --------------------------------------------------------------------------- doc
def _cmd_doc(args: argparse.Namespace) -> int:
    try:
        suite = load_file(args.suite)
    except SuiteConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.suite)), "MCP-TEST-SUITE.md")
    write_doc(suite, out)
    print(f"已渲染 {out}（{len(suite.cases)} 条用例）")
    return 0


# --------------------------------------------------------------------------- 小工具
def _split_multi(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _write(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _short(value) -> str:
    import json as _json

    if isinstance(value, str):
        text = value
    else:
        text = _json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= 200 else text[:200] + "…"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
