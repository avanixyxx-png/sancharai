"""
NVIDIA NIM as the AI brain.

Reported: live test calls were cut, with the workspace on NVIDIA's Nemotron 3
models. The app only sent chat_template_kwargs {"thinking": False}, which is
the Nemotron 2 spelling - Nemotron 3 / 3.5 read "enable_thinking" and reason
BY DEFAULT otherwise, slow enough to drop a call and liable to put reasoning
text into what the caller hears. The old default (nvidia-nemotron-nano-9b-v2)
had also been retired from NVIDIA's API. The OpenAI client is mocked: no
NVIDIA key or network is needed.
"""
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
os.environ.pop("DATABASE_URL", None)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_nvidia.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import engine  # noqa: E402
from models import CompanySettings  # noqa: E402
from security import encrypt  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def call(content, model=""):
    s = CompanySettings(company_id=1, ai_provider="nvidia",
                        nvidia_api_key_enc=encrypt("nvapi-test"), nvidia_model=model)
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=content))]
    with patch("openai.OpenAI") as client:
        client.return_value.chat.completions.create.return_value = fake
        out = engine._llm(s, [{"role": "user", "content": "hi"}], 60)
        return out, client.call_args.kwargs, client.return_value.chat.completions.create.call_args.kwargs


out, ctor, kw = call("Hello, how can I help?")
check("talks to NVIDIA's OpenAI-compatible endpoint",
      ctor.get("base_url") == "https://integrate.api.nvidia.com/v1", str(ctor))
check("default model is one NVIDIA still serves (nemotron-3.5-lightning)",
      kw["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b", kw["model"])
tk = kw.get("extra_body", {}).get("chat_template_kwargs", {})
check("THE FIX: thinking disabled with the Nemotron 3 key (enable_thinking)",
      tk.get("enable_thinking") is False, str(tk))
check("...and the older key kept for Nemotron 2 models", tk.get("thinking") is False, str(tk))
check("a plain reply passes through untouched", out == "Hello, how can I help?", repr(out))

_, _, kw = call("ok", model="nvidia/nemotron-3-super-120b-a12b")
check("a model chosen in Settings is used instead of the default",
      kw["model"] == "nvidia/nemotron-3-super-120b-a12b", kw["model"])

out, _, _ = call("<think>The user said hi, I should greet them politely.</think>\nHello!")
check("reasoning in <think> tags is never spoken to the caller", out == "Hello!", repr(out))
out, _, _ = call("plan the reply first...</think> Namaste!")
check("reasoning with only a closing tag is stripped too", out == "Namaste!", repr(out))

with patch("openai.OpenAI"):
    try:
        engine._llm(CompanySettings(company_id=1, ai_provider="nvidia"), [], 10)
        raised = False
    except RuntimeError:
        raised = True
check("a missing NVIDIA key fails clearly instead of calling the API", raised)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
