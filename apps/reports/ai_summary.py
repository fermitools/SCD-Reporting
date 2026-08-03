"""Generate an AI-written narrative summary of a WorkItem queryset via the Anthropic API."""
import os

from django.conf import settings

from .exporters import _rows


def _format_entries(qs) -> str:
    parts = []
    for r in _rows(qs):
        flags = []
        if r['critical'] == 'yes':
            flags.append('CRITICAL')
        if r['highlight'] == 'yes':
            stars = '★' * int(r['highlight_stars']) if r['highlight_stars'] else ''
            flags.append(f'HIGHLIGHT {stars}'.strip())
        if r['private'] == 'yes':
            flags.append('PRIVATE')
        flag_str = f'  [{", ".join(flags)}]' if flags else ''
        group_str = f'  |  group: {r["group"]}' if r['group'] else ''
        tags_str = f'  |  tags: {r["tags"]}' if r['tags'] else ''
        desc_str = f'\n    {r["description"]}' if r['description'] else ''
        parts.append(
            f'- [{r["period_start"]} – {r["period_end"]}] {r["title"]}{flag_str}\n'
            f'  Author: {r["author"]}  |  {r["project"]} / {r["category"]}'
            + group_str
            + tags_str
            + desc_str
        )
    return '\n'.join(parts) if parts else '(no entries)'


def generate(qs, user=None, template=None) -> str:
    """Call the Anthropic API and return the summary as a Markdown string.

    template: a NamedPromptTemplate instance; overrides the user's default config when provided.
    """
    import anthropic
    from .models import AIPromptConfig

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise ValueError(
            'ANTHROPIC_API_KEY is not configured. '
            'Set it as an environment variable or in Django settings.'
        )

    if template is not None:
        system_prompt = template.system_prompt
        user_template = template.user_template
    else:
        config = AIPromptConfig.for_user(user) if user is not None else AIPromptConfig.get_solo()
        system_prompt = config.system_prompt
        user_template = config.user_template

    model = settings.ANTHROPIC_SUMMARY_MODEL
    base_url = settings.ANTHROPIC_BASE_URL or None
    client = anthropic.Anthropic(api_key=api_key, **({"base_url": base_url} if base_url else {}))
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_template.format(entries=_format_entries(qs))}],
    )
    text_blocks = [block.text for block in message.content if block.type == 'text']
    return '\n'.join(text_blocks)
