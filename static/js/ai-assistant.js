/* ═══════════════════════════════════════════════════════════════
   AREAPULSE AI ASSISTANT — ai-assistant.js
   Chat UI · Streaming · Workspace · Voice integration
   ═══════════════════════════════════════════════════════════════ */

'use strict';

class AIAssistant {
  constructor(role, containerId, workspaceId) {
    this.role        = role || 'gov';
    this.container   = document.getElementById(containerId);
    this.workspace   = document.getElementById(workspaceId);
    this.messages    = document.getElementById('ai-messages');
    this.input       = document.getElementById('ai-input');
    this.sendBtn     = document.getElementById('ai-send-btn');
    this.history     = [];
    this.voiceMode   = false;
    this.voiceAssist = null;

    this._initVoice();
    this._bindEvents();
    this._showWelcome();
  }

  _initVoice() {
    try {
      this.voiceAssist = new AreaPulseVoiceAssistant(this.role, (transcript) => {
        if (this.input) this.input.value = transcript;
        this.sendMessage(transcript);
      });
    } catch (e) { console.warn('[AI] Voice init failed:', e); }
  }

  _bindEvents() {
    this.sendBtn?.addEventListener('click', () => this._send());
    this.input?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
    });
    this.input?.addEventListener('input', () => {
      this.input.style.height = 'auto';
      this.input.style.height = Math.min(this.input.scrollHeight, 120) + 'px';
    });
    document.getElementById('ai-voice-btn')?.addEventListener('click', () => {
      this.voiceAssist?.toggle();
    });
    document.querySelectorAll('[data-shortcut]').forEach(btn => {
      btn.addEventListener('click', () => this.sendMessage(btn.dataset.shortcut));
    });
  }

  _send() {
    const text = this.input?.value.trim();
    if (!text) return;
    if (this.input) this.input.value = '';
    if (this.input) this.input.style.height = 'auto';
    this.sendMessage(text);
  }

  async sendMessage(userText) {
    if (!userText?.trim()) return;

    // 1. Add user bubble
    this._addBubble('user', userText);
    this.history.push({ role: 'user', content: userText });

    // 2. Thinking indicator
    const thinkingEl = this._addThinking();

    // 3. Determine endpoint
    const endpoint = `/${this.role}/ai-chat`;

    let fullResponse = '';
    const aiEl = this._addBubble('ai', '');

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userText, history: this.history.slice(-10) })
      });

      thinkingEl.remove();

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      // Stream response
      if (res.body && res.headers.get('content-type')?.includes('text/event-stream')) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          chunk.split('\n').forEach(line => {
            if (line.startsWith('data: ')) {
              try {
                const d = JSON.parse(line.slice(6));
                const content = d.content || d.chunk || '';
                fullResponse += content;
                aiEl.innerHTML = this._renderMarkdown(fullResponse);
                this._scrollToBottom();
              } catch {}
            }
          });
        }
      } else {
        // Non-streaming fallback
        const data = await res.json();
        fullResponse = data.response || data.answer || data.content || 'No response';
        aiEl.innerHTML = this._renderMarkdown(fullResponse);
      }

      // 4. Parse special actions
      this._parseActions(fullResponse);

      // 5. Store in history
      this.history.push({ role: 'assistant', content: fullResponse });

      // 6. Speak if voice mode
      if (this.voiceMode && this.voiceAssist) {
        const summary = fullResponse.slice(0, 200).replace(/[#*`]/g, '');
        this.voiceAssist.speak(summary);
      }

    } catch (err) {
      thinkingEl?.remove();
      aiEl.innerHTML = `<span style="color:var(--red)">Error: ${err.message}</span>`;
      Toast.error('AI request failed: ' + err.message);
    }

    this._scrollToBottom();
  }

  _addBubble(role, content) {
    if (!this.messages) return document.createElement('div');
    const el = document.createElement('div');
    el.className = `msg-bubble ${role}`;
    el.innerHTML = content ? this._renderMarkdown(content) : '';
    this.messages.appendChild(el);
    this._scrollToBottom();
    return el;
  }

  _addThinking() {
    if (!this.messages) return document.createElement('div');
    const el = document.createElement('div');
    el.className = 'msg-thinking';
    el.innerHTML = `
      <div class="msg-dot"></div>
      <div class="msg-dot"></div>
      <div class="msg-dot"></div>
      <span style="font-size:11px;color:var(--ink-3);margin-left:4px">AreaPulse AI is thinking...</span>`;
    this.messages.appendChild(el);
    this._scrollToBottom();
    return el;
  }

  _scrollToBottom() {
    if (this.messages) this.messages.scrollTop = this.messages.scrollHeight;
  }

  _renderMarkdown(text) {
    if (!text) return '';
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/`(.*?)`/g, '<code style="background:var(--bg-subtle);padding:1px 5px;border-radius:4px;font-size:12px">$1</code>')
      .replace(/^### (.*)/gm, '<h3 style="font-size:14px;font-weight:700;margin:10px 0 4px;color:var(--ink)">$1</h3>')
      .replace(/^## (.*)/gm,  '<h2 style="font-size:15px;font-weight:700;margin:12px 0 6px;color:var(--ink)">$1</h2>')
      .replace(/^# (.*)/gm,   '<h1 style="font-size:18px;font-weight:700;margin:12px 0 8px;color:var(--ink)">$1</h1>')
      .replace(/^- (.*)/gm,   '<li style="margin:2px 0;padding-left:8px">• $1</li>')
      .replace(/^(\d+)\. (.*)/gm, '<li style="margin:2px 0;padding-left:8px">$1. $2</li>')
      .replace(/\n\n/g, '</p><p style="margin:6px 0">')
      .replace(/\n/g, '<br>');
  }

  _parseActions(response) {
    if (!this.workspace) return;
    const lower = response.toLowerCase();

    if (lower.includes('[map]') || lower.includes('on a map') || lower.includes('mapped')) {
      this._renderWorkspaceMap();
    } else if (lower.includes('[chart]') || lower.includes('chart') || lower.includes('graph')) {
      this._renderWorkspaceChart();
    } else if (lower.includes('[whatsapp]') || lower.includes('whatsapp message') || lower.includes('send a message')) {
      this._renderWorkspaceWhatsApp(response);
    } else if (lower.includes('[report]') || lower.includes('report is ready') || lower.includes('generate a report')) {
      this._renderWorkspaceReport(response);
    } else {
      // Default: show formatted text
      this.workspace.innerHTML = `
        <div style="padding:20px;background:var(--bg-subtle);border-radius:var(--r-lg);border:1px solid var(--border);font-size:13px;line-height:1.7;color:var(--ink-2)">
          ${this._renderMarkdown(response)}
        </div>`;
    }
  }

  _renderWorkspaceMap() {
    this.workspace.innerHTML = `
      <div style="border-radius:var(--r-lg);overflow:hidden;height:360px">
        <div id="ai-workspace-map" style="height:100%"></div>
      </div>`;
    setTimeout(() => {
      const map = MapEngine.initMap('ai-workspace-map');
      if (map) API.get(`/${this.role}/api/issues`).then(data => {
        MapEngine.renderIssues(map, data.issues || [], this.role);
      });
    }, 100);
  }

  _renderWorkspaceChart() {
    this.workspace.innerHTML = `
      <div style="padding:20px;background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg)">
        <div style="height:280px;position:relative"><canvas id="ai-workspace-chart"></canvas></div>
      </div>`;
    setTimeout(() => {
      const labels = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
      const data   = labels.map(() => Math.floor(Math.random() * 30 + 10));
      ChartBuilders.buildSparkline('ai-workspace-chart', data);
    }, 100);
  }

  _renderWorkspaceWhatsApp(response) {
    // Extract a draft message from response
    const draftMatch = response.match(/[""]([^""]{20,})[""]/);
    const draft = draftMatch ? draftMatch[1] : 'Dear citizen, your reported issue is being addressed. — AreaPulse Team';
    this.workspace.innerHTML = `
      <div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg);overflow:hidden">
        <div style="background:#25D366;padding:12px 16px;color:#fff;font-weight:700;display:flex;align-items:center;gap:8px">
          <span>📱</span> WhatsApp Message Draft
        </div>
        <div style="padding:16px">
          <textarea id="wa-draft" style="width:100%;min-height:100px;border:1px solid var(--border);border-radius:8px;padding:10px;font-size:13px;color:var(--ink);background:var(--bg-subtle);resize:vertical">${draft}</textarea>
          <div style="display:flex;gap:10px;margin-top:12px">
            <input id="wa-phone" placeholder="+91 XXXXX XXXXX" style="flex:1;padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px">
            <button onclick="sendWhatsAppFromWorkspace()" style="padding:8px 20px;background:#25D366;color:#fff;border:none;border-radius:8px;font-weight:700;cursor:pointer">📤 Send</button>
          </div>
        </div>
      </div>`;
  }

  _renderWorkspaceReport(response) {
    this.workspace.innerHTML = `
      <div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r-lg);overflow:hidden">
        <div style="padding:16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
          <div style="font-weight:700;font-size:15px">📄 AI Generated Report</div>
          <div style="display:flex;gap:8px">
            <a href="/gov/api/export-pdf?type=ai" class="btn btn-primary btn-sm" style="text-decoration:none">⬇ PDF</a>
            <a href="/gov/api/export-csv"         class="btn btn-secondary btn-sm" style="text-decoration:none">📊 CSV</a>
          </div>
        </div>
        <div style="padding:20px;font-size:13px;color:var(--ink-2);line-height:1.7;max-height:400px;overflow-y:auto">
          ${this._renderMarkdown(response)}
        </div>
      </div>`;
  }

  _showWelcome() {
    const roleLabel = this.role === 'gov' ? 'Government Officer' : 'NGO Partner';
    const examples  = this.role === 'gov'
      ? ['"Summarize all overdue issues"', '"Draft a WhatsApp response for AP-3142"', '"Which area has the most water complaints?"']
      : ['"Where should I deploy volunteers this weekend?"', '"Which issue in Lajpat Nagar has highest impact?"', '"Find partner NGOs working on water in Dwarka"'];

    if (!this.messages) return;
    const el = document.createElement('div');
    el.className = 'msg-bubble ai';
    el.innerHTML = `
      <div style="font-weight:700;margin-bottom:8px">👋 Hello, ${roleLabel}!</div>
      <div style="font-size:12px;color:var(--ink-3);margin-bottom:10px">I'm your AreaPulse AI assistant, powered by Groq Llama. Ask me anything about your civic data.</div>
      <div style="font-size:12px;color:var(--ink-3);font-weight:600;margin-bottom:6px">Try asking:</div>
      ${examples.map(ex => `<div style="margin:4px 0;font-size:12px;color:var(--accent);cursor:pointer" onclick="aiAssistant.sendMessage(${ex})">${ex}</div>`).join('')}`;
    this.messages.appendChild(el);
  }

  setVoiceMode(on) {
    this.voiceMode = on;
  }
}

// ── WhatsApp from workspace ───────────────────────────────────
window.sendWhatsAppFromWorkspace = async function() {
  const phone = document.getElementById('wa-phone')?.value;
  const msg   = document.getElementById('wa-draft')?.value;
  if (!phone || !msg) { Toast.warning('Enter phone number and message'); return; }
  try {
    await API.whatsapp.send(phone, msg);
    Toast.success('WhatsApp message sent!');
  } catch (e) { Toast.error('Send failed: ' + e.message); }
};

window.AIAssistant = AIAssistant;
