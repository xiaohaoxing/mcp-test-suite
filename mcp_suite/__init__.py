"""MCP Test Suite —— 把「MCP 能力验收」变成可维护、可执行、可对比的资产。

模块分工（一层一件事，互不反向依赖）：

    loader.py     读 YAML + 校验 schema（唯一入口，所有结构错误在这里变成退出码 2）
    runner.py     执行 tool / http 两类动作（MCP Streamable HTTP + 普通 HTTP）
    assertions.py 把 expect 的 12 个断言键变成 pass/fail + 「期望 vs 实际」
    report.py     JSON / Markdown / 控制台三种视图（覆盖矩阵 + 优先级汇总）
    emit_doc.py   YAML → MCP-TEST-SUITE.md（同一份事实的两种读法）
    cli.py        python -m mcp_suite run|validate|doc

设计约束（改代码前先读）：

· **YAML 是唯一事实来源**。MCP-TEST-SUITE.md 由 emit_doc 渲染，不手写；
  手写两份必然漂移，而漂移的验收文档比没有文档更糟（会让人相信已覆盖）。
· **令牌只从环境变量取**。本包任何地方都不打印完整令牌；报告里只有前 8 位。
· **单条用例失败不中断整轮**。验收的价值在「这一轮到底哪些不成立」，
  第一条就抛异常的做法在真实故障里只会给出 1/N 的信息。
"""

__version__ = "0.1.0"

#: 报告里令牌只显示前这么多位（约束：绝不把完整令牌写进任何产物）
TOKEN_MASK_PREFIX = 8
