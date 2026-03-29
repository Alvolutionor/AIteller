# src/utils/llm.py
import asyncio
import logging
import os
import tempfile
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Git bash path for claude CLI on Windows
_GIT_BASH_PATH = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "bash")


class LLMClient:
    def __init__(self, config: dict, dry_run: bool = False):
        llm_cfg = config["llm"]
        self.primary = llm_cfg["primary"]
        self.fallback = llm_cfg["fallback"]
        self.dry_run = dry_run
        self._token_budget = llm_cfg.get("daily_token_budget", 500000)
        self._tokens_used = 0

        self._claude_cfg = llm_cfg.get("claude", {})
        self._openai_cfg = llm_cfg.get("openai", {})
        self._deepseek_cfg = llm_cfg.get("deepseek", {})
        self._local_cfg = llm_cfg.get("local", {})
        self._claude_cli_cfg = llm_cfg.get("claude_cli", {})
        self._claude_proxy_cfg = llm_cfg.get("claude_proxy", {})

        # Claude API
        if self._claude_cfg.get("api_key"):
            self._claude = AsyncAnthropic(api_key=self._claude_cfg["api_key"])
        else:
            self._claude = None

        # OpenAI
        if self._openai_cfg.get("api_key"):
            self._openai = AsyncOpenAI(api_key=self._openai_cfg["api_key"])
        else:
            self._openai = None

        # DeepSeek (OpenAI-compatible API)
        if self._deepseek_cfg.get("api_key"):
            self._deepseek = AsyncOpenAI(
                api_key=self._deepseek_cfg["api_key"],
                base_url=self._deepseek_cfg.get("base_url", "https://api.deepseek.com"),
            )
        else:
            self._deepseek = None

        # Local model via Ollama/LM Studio (OpenAI-compatible)
        if self._local_cfg.get("enabled"):
            self._local = AsyncOpenAI(
                api_key="not-needed",
                base_url=self._local_cfg.get("base_url", "http://localhost:11434/v1"),
            )
        else:
            self._local = None

        # Claude proxy (OpenAI-compatible, e.g. ClaudeCLIProxy on localhost)
        if self._claude_proxy_cfg.get("base_url"):
            self._claude_proxy = AsyncOpenAI(
                api_key=self._claude_proxy_cfg.get("api_key", "not-needed"),
                base_url=self._claude_proxy_cfg["base_url"],
            )
        else:
            self._claude_proxy = None

    def _get_model(self, provider: str, tier: str) -> str:
        cfg_map = {
            "claude": self._claude_cfg,
            "openai": self._openai_cfg,
            "deepseek": self._deepseek_cfg,
            "local": self._local_cfg,
            "claude_cli": self._claude_cli_cfg,
            "claude_proxy": self._claude_proxy_cfg,
        }
        cfg = cfg_map.get(provider, {})
        return cfg.get("fast_model") if tier == "fast" else cfg.get("standard_model", "")

    async def complete(self, prompt: str, model_tier: str = "standard") -> str:
        if self.dry_run:
            return f"[DRY RUN] Mock response for: {prompt[:50]}..."

        if self._token_budget > 0 and self._tokens_used >= self._token_budget:
            raise RuntimeError("Daily token budget exhausted")

        try:
            return await self._call_provider(self.primary, prompt, model_tier)
        except Exception as primary_exc:
            logger.warning("Primary (%s) failed: %s, falling back to %s",
                           self.primary, type(primary_exc).__name__, self.fallback)
            try:
                return await self._call_provider(self.fallback, prompt, model_tier)
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"Both providers failed. Primary ({self.primary}): {primary_exc}. "
                    f"Fallback ({self.fallback}): {fallback_exc}"
                ) from fallback_exc

    async def _call_provider(self, provider: str, prompt: str, tier: str) -> str:
        if provider == "claude":
            return await self._call_claude(prompt, tier)
        elif provider == "claude_cli":
            return await self._call_claude_cli(prompt, tier)
        elif provider == "claude_proxy":
            return await self._call_openai_compat(self._claude_proxy, "claude_proxy", prompt, tier)
        elif provider == "deepseek":
            return await self._call_openai_compat(self._deepseek, "deepseek", prompt, tier)
        elif provider == "local":
            return await self._call_openai_compat(self._local, "local", prompt, tier)
        else:
            return await self._call_openai_compat(self._openai, "openai", prompt, tier)

    async def _call_claude(self, prompt: str, tier: str) -> str:
        if self._claude is None:
            raise RuntimeError("Claude client not configured (missing api_key)")
        model = self._get_model("claude", tier)
        response = await self._claude.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        tokens = response.usage.input_tokens + response.usage.output_tokens
        self._tokens_used += tokens
        logger.info("Claude (%s): %d tokens", model, tokens)
        return response.content[0].text

    async def _call_claude_cli(self, prompt: str, tier: str) -> str:
        """Call Claude via the claude CLI pipe mode (uses subscription, no API charge)."""
        model = self._get_model("claude_cli", tier)
        model_args = ["--model", model] if model else []

        # Write prompt to temp file to avoid shell arg length limits
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)  # allow nested invocation
            env["CLAUDE_CODE_GIT_BASH_PATH"] = _GIT_BASH_PATH

            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", *model_args, "--stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            with open(prompt_file, "rb") as pf:
                prompt_bytes = pf.read()

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt_bytes),
                timeout=120,
            )

            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"claude CLI exited with code {proc.returncode}: {err_msg}")

            result = stdout.decode("utf-8", errors="replace").strip()
            logger.info("claude_cli (%s): response length %d chars", model or "default", len(result))
            return result
        finally:
            os.unlink(prompt_file)

    async def _call_openai_compat(self, client, provider_name: str, prompt: str, tier: str) -> str:
        if client is None:
            raise RuntimeError(f"{provider_name} client not configured")
        model = self._get_model(provider_name, tier)
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
        )
        tokens = (response.usage.prompt_tokens + response.usage.completion_tokens) if response.usage else 0
        self._tokens_used += tokens
        logger.info("%s (%s): %d tokens", provider_name, model, tokens)
        return response.choices[0].message.content
