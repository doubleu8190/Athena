"""校验 Run 协议版本并返回其固定的系统提示词正文。"""

from __future__ import annotations

from domain.run.model import Run


def system_prompt_for_run(run: Run, content: str) -> str:
    """校验执行协议后返回 Run 绑定的不可变正文。"""
    if run.prompt_template_version != "run-v2":
        raise ValueError("run_prompt_template_unsupported")
    return content


__all__ = ["system_prompt_for_run"]
