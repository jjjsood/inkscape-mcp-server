"""MCP Prompt library (architecture §4.1).

Prompts orient the agent on how to use the tool surface SAFELY; they grant no capability of their
own. Each module decorates functions with ``@mcp.prompt`` against the shared app and self-registers
on import (wired from ``server.register_tools``).
"""
