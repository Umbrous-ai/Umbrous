"""Construct the same explicitly configured providers for desktop, HTTP and CLI."""
from umbrous.config import Config
from umbrous.model.base import Deadlines
from umbrous.model.ollama import OllamaAdapter
from umbrous.model.router import ModelRouter


def create_router(config: Config) -> ModelRouter:
    deadlines = Deadlines(first_token=config.first_token_timeout_s,
                          idle=config.idle_timeout_s, total=config.total_timeout_s)
    codex = None
    if config.codex_enabled:
        from umbrous.model.codex import CodexAdapter
        codex = CodexAdapter(config.home, deadlines=deadlines)
    claude = None
    if config.claude_enabled:
        from umbrous.model.claude import ClaudeAdapter
        claude = ClaudeAdapter(config.home, deadlines=deadlines)
    grok = None
    if config.grok_enabled:
        from umbrous.model.grok import GrokAdapter
        grok = GrokAdapter(config.home, deadlines=deadlines)
    return ModelRouter(OllamaAdapter(config.ollama_url, deadlines), config.db_path, codex=codex, claude=claude,
                       grok=grok)
