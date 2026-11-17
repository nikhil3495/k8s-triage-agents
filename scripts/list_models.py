"""List the Gemini models this API key can use, flagging the configured defaults."""

from __future__ import annotations

from google import genai

from triage.config import get_settings


def main() -> None:
    s = get_settings()
    key = s.gemini_api_key.get_secret_value() if s.gemini_api_key else None
    client = genai.Client(api_key=key) if key else genai.Client()
    wanted = {s.supervisor_model, s.worker_model, s.critic_model, s.embedding_model}
    seen = set()
    for m in client.models.list():
        name = (m.name or "").removeprefix("models/")
        if "gemini" in name:
            seen.add(name)
            print(("* " if name in wanted else "  ") + name)
    missing = wanted - seen
    print("configured models available" if not missing else f"MISSING: {sorted(missing)}")


if __name__ == "__main__":
    main()
