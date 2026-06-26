"""
AreaPulse Portal — ai_engine.py
Groq llama-3.3-70b-versatile integration.
Role-aware system prompts (Gov / NGO).
SSE streaming + non-streaming fallback.
"""
import os
import json
import time

try:
    from groq import Groq
    _client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))
except Exception:
    _client = None

MODEL = 'llama-3.3-70b-versatile'


def _build_system_prompt(role, user_info, issues_snapshot=None):
    """Build a role-aware system prompt injecting live DB context."""
    base = (
        "You are AreaPulse AI — a smart civic intelligence assistant for Delhi, India. "
        "You help stakeholders manage, understand, and act on civic issues reported by Delhi citizens. "
        "Always be concise, actionable, and specific. Cite area names and issue types when relevant. "
        "Respond in 2-6 sentences unless a longer structured answer is explicitly needed. "
        "You can output [MAP], [CHART], [WHATSAPP], or [REPORT] tags to trigger workspace rendering."
    )

    if role == 'gov':
        role_ctx = (
            f"\n\nYou are assisting {user_info.get('name', 'a Government Officer')} "
            f"from {user_info.get('dept', 'Delhi Government')}. "
            f"Their department handles: {', '.join(user_info.get('tags', []))}. "
            "Help them: prioritise issues, draft WhatsApp notifications to citizens, "
            "understand SLA compliance, escalate urgent cases, and generate reports."
        )
    else:
        role_ctx = (
            f"\n\nYou are assisting {user_info.get('name', 'an NGO Partner')} "
            f"focused on: {user_info.get('dept', 'civic issues')}. "
            "Help them: find matching civic opportunities, plan volunteer deployment, "
            "track impact, coordinate with government, and generate donor reports."
        )

    context = ''
    if issues_snapshot:
        context = f'\n\nLive issues snapshot ({len(issues_snapshot)} issues):\n'
        for i in issues_snapshot[:20]:
            sla = i.get('sla_state', 'unknown')
            context += (
                f"- [#{i.get('id')}] {i.get('tag','?').upper()} "
                f"| {i.get('area','?')} | {i.get('severity','?')} | "
                f"Status: {i.get('status','?')} | SLA: {sla} "
                f"| {(i.get('description',''))[:60]}\n"
            )

    return base + role_ctx + context


def chat(message, role, user_info, history=None, issues_snapshot=None):
    """
    Non-streaming chat. Returns {'response': str, 'source': str}.
    """
    if not _client:
        return {
            'response': (
                'AI is not configured. Set GROQ_API_KEY environment variable. '
                'In demo mode, I can tell you that this portal shows '
                f'{len(issues_snapshot or [])} active civic issues across Delhi.'
            ),
            'source': 'demo'
        }

    system_prompt = _build_system_prompt(role, user_info, issues_snapshot)
    messages = [{'role': 'system', 'content': system_prompt}]

    # Add history (last 8 turns max)
    for h in (history or [])[-8:]:
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})

    messages.append({'role': 'user', 'content': message})

    try:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=600,
            temperature=0.3,
        )
        answer = (resp.choices[0].message.content or '').strip()
        return {'response': answer, 'source': f'groq-{MODEL}'}
    except Exception as e:
        return {'response': f'AI error: {type(e).__name__}. Please try again.', 'source': 'error'}


def chat_stream(message, role, user_info, history=None, issues_snapshot=None):
    """
    Generator that yields SSE-formatted chunks.
    Usage: yield from chat_stream(...)
    """
    if not _client:
        demo_response = (
            f'AI demo mode (no GROQ_API_KEY). '
            f'In a live deployment, I would analyse {len(issues_snapshot or [])} '
            f'active Delhi civic issues and give you specific recommendations.'
        )
        for word in demo_response.split():
            yield f'data: {json.dumps({"content": word + " "})}\n\n'
            time.sleep(0.04)
        yield 'data: [DONE]\n\n'
        return

    system_prompt = _build_system_prompt(role, user_info, issues_snapshot)
    messages = [{'role': 'system', 'content': system_prompt}]
    for h in (history or [])[-8:]:
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': message})

    try:
        stream = _client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=600,
            temperature=0.3,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield f'data: {json.dumps({"content": delta})}\n\n'
        yield 'data: [DONE]\n\n'
    except Exception as e:
        yield f'data: {json.dumps({"content": f"Error: {type(e).__name__}"})}\n\n'
        yield 'data: [DONE]\n\n'


def gov_briefing(issues):
    """
    AI morning briefing for Gov dashboard.
    Returns a 2-3 sentence executive summary.
    """
    if not _client or not issues:
        breached = sum(1 for i in issues if i.get('sla_state') == 'breached')
        critical = sum(1 for i in issues if i.get('sla_state') == 'critical')
        return (
            f'Good morning. You have <strong>{len(issues)} active issues</strong> in your queue. '
            f'<strong>{breached} are SLA-breached</strong> and {critical} are critical — '
            'prioritise these immediately. Connect GROQ_API_KEY for full AI briefings.'
        )

    open_issues = [i for i in issues if i.get('status') not in ('resolved',)]
    breached    = [i for i in open_issues if i.get('sla_state') == 'breached']
    by_area     = {}
    for i in open_issues:
        a = i.get('area', 'Unknown')
        by_area[a] = by_area.get(a, 0) + 1
    top_area = max(by_area, key=by_area.get) if by_area else 'N/A'

    stats = (
        f"Total open: {len(open_issues)}. "
        f"SLA breached: {len(breached)}. "
        f"Hotspot area: {top_area} ({by_area.get(top_area, 0)} issues). "
        f"Top categories: {_top_tags(open_issues)}."
    )

    try:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': 'You are a civic analytics AI for Delhi Government. Write a 2-3 sentence executive briefing highlighting the most urgent pattern and one specific recommendation. Be direct, use numbers.'},
                {'role': 'user', 'content': f'Issue stats: {stats}'},
            ],
            max_tokens=120,
            temperature=0.2,
        )
        return (resp.choices[0].message.content or '').strip()
    except Exception as e:
        return f'AI briefing unavailable ({type(e).__name__}). {len(breached)} SLA breaches require immediate attention.'


def ngo_recommend(issues, ngo_tags, ngo_area):
    """
    AI recommendation for NGO: which issues to pick up.
    Returns a 2-3 sentence recommendation string.
    """
    if not _client or not issues:
        matched = [i for i in issues if i.get('tag') in ngo_tags]
        return (
            f'Found <strong>{len(matched)} issues</strong> matching your focus areas. '
            f'High-priority ones in {ngo_area} are ready for your team. '
            'Connect GROQ_API_KEY for personalised AI recommendations.'
        )

    matching = [i for i in issues if i.get('tag') in ngo_tags and i.get('status') != 'resolved'][:10]
    issue_list = '\n'.join(
        f"- #{i.get('id')} {i.get('tag')} in {i.get('area')}: {(i.get('description',''))[:60]}"
        for i in matching
    )

    try:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': f'You are an NGO deployment advisor for Delhi. This NGO focuses on {", ".join(ngo_tags)} in {ngo_area}. Recommend 1-2 specific issues to act on and why. Be specific, use issue numbers.'},
                {'role': 'user', 'content': f'Available issues:\n{issue_list}'},
            ],
            max_tokens=100,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or '').strip()
    except Exception as e:
        return f'AI recommendations unavailable. {len(matching)} matching issues found in your focus area.'


def _top_tags(issues, n=3):
    counts = {}
    for i in issues:
        t = i.get('tag', 'other')
        counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:n]
    return ', '.join(f'{t}({c})' for t, c in top)
