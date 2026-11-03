'[Deployment-specific example removed from this source docstring.]'
import os

# Default institutional voice. Every briefing speaks the same way unless a caller
# overrides `system`. Keeps Trends, Explorer, and chart descriptions consistent.
SYSTEM_ANALYST = (
    "You are a legislative policy analyst at the University of Illinois System, "
    "writing for administrators and government-relations staff. Write in clear, "
    "plain English for a smart non-technical reader. Be specific and concrete, use "
    "active voice, and keep it tight. Write short prose paragraphs, not bullet "
    "points or headers, unless explicitly asked. Never invent facts beyond the data "
    "you are given; if the data is thin or ambiguous, say so plainly rather than "
    "guessing."
)

# GA-stable version that supports gpt-4o-mini chat completions. Overridable via env.
_DEFAULT_API_VERSION = "2024-08-01-preview"


def _get_config() -> dict:
    """Read the Azure OpenAI settings from the environment each call (cheap, and
    picks up App Service restarts without a process reload)."""
    return {
        "endpoint":    os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip(),
        "key":         os.environ.get("AZURE_OPENAI_KEY", "").strip(),
        "deployment":  os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini").strip(),
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", _DEFAULT_API_VERSION).strip(),
    }


def is_configured() -> bool:
    """True only when both the endpoint and key are present. Pages gate the
    'Generate' button on this so the feature ships dormant and safe."""
    cfg = _get_config()
    return bool(cfg["endpoint"] and cfg["key"])


def _client():
    """Construct the AzureOpenAI client. Imported lazily so the module loads
    even in an environment where openai isn't installed yet."""
    from openai import AzureOpenAI
    cfg = _get_config()
    return AzureOpenAI(
        azure_endpoint=cfg["endpoint"],
        api_key=cfg["key"],
        api_version=cfg["api_version"],
    )


def generate(prompt: str,
             system: str = SYSTEM_ANALYST,
             max_tokens: int = 500,
             temperature: float = 0.4) -> str:
    """
    Send one prompt to Azure OpenAI and return the model's text.

    Raises RuntimeError if not configured, and lets SDK/network exceptions
    propagate. Streamlit callers should wrap this in try/except and surface the
    message with st.error(), e.g.:

        try:
            with st.spinner("Generating..."):
                text = ai_briefing.generate(prompt)
            st.markdown(text)
        except Exception as e:
            st.error(f"Briefing generation failed: {e}")
    """
    if not is_configured():
        raise RuntimeError(
            "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and "
            "AZURE_OPENAI_KEY in App Service -> Environment variables."
        )

    cfg = _get_config()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = _client().chat.completions.create(
        model=cfg["deployment"],   # Azure routes on the *deployment* name, not the model id
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


def describe_chart(chart_title: str, data_summary: str, max_tokens: int = 220) -> str:
    """
    Plain-English takeaway for a chart, grounded in its underlying numbers.
    Powers both the on-click "Describe this chart" button and serves as the
    spoken equivalent of seeing the chart for non-sighted users.

    The data_summary should put any ranking/extreme values under a "KEY FACTS"
    heading; this function instructs the model to use those verbatim rather than
    computing maxima/minima itself (small models are unreliable at that).
    """
    prompt = (
        f'A reader is looking at a chart titled "{chart_title}" on a legislative '
        f'analytics dashboard for the University of Illinois.\n\n'
        f'{data_summary}\n\n'
        # Audit bug 15: this text is the chart's accessible description for
        # screen-reader users. The auditor's finding was that it "interprets"
        # the chart rather than describing it — a disabled user should get an
        # objective account and draw their own conclusions, the same as a
        # sighted user looking at the chart. So: describe, do not editorialise.
        f'Write 3-4 sentences that DESCRIBE what this chart shows, objectively, '
        f'for someone who cannot see it. State the chart type, what the axes or '
        f'categories represent, the range of values, and the highest and lowest '
        f'items using the KEY FACTS. You may note one clearly visible pattern '
        f'(e.g. "values rise steadily", "one category is far above the rest") '
        f'as a neutral observation. Do NOT say what the reader should notice, '
        f'what matters, what it means for policy, or what conclusion to draw. '
        f'No takeaways, no recommendations, no significance claims.\n\n'
        f'CRITICAL RULES:\n'
        f'- Base every number and comparison ONLY on the figures provided above.\n'
        f'- If a "KEY FACTS" section is present, use it verbatim for any '
        f'"highest/lowest/most/largest/biggest" statement. Do NOT rank, sort, '
        f'count, or do arithmetic yourself — you will make mistakes.\n'
        f'- Do not invent categories, bills, or numbers not listed above.\n'
        f'- Do not describe chart mechanics or restate every number.'
    )
    return generate(prompt, max_tokens=max_tokens)
