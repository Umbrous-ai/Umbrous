"""Model Router + Adapters (M1): ollama.py talks to Ollama, router.py picks the model
(installed only, never a fallback), probe.py checks tool calls, exposure.py checks
whether Ollama is reachable from the network. See docs/m1-chat.md."""
