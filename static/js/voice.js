/* ═══════════════════════════════════════════════════════════════
   AREAPULSE VOICE ASSISTANT — voice.js
   Web Speech API · Voice UI · TTS · Wake word
   ═══════════════════════════════════════════════════════════════ */

'use strict';

class AreaPulseVoiceAssistant {
  constructor(role, onTranscript) {
    this.role        = role || 'gov';
    this.onTranscript = onTranscript || (() => {});
    this.isListening  = false;
    this.isSpeaking   = false;
    this.recognition  = null;
    this.synthesis    = window.speechSynthesis;
    this.voices       = [];
    this.transcript   = '';
    this.silenceTimer = null;

    this._initRecognition();
    this._loadVoices();
    this._buildUI();
    this._bindShortcuts();
  }

  _initRecognition() {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) { console.warn('[Voice] SpeechRecognition not available'); return; }

    this.recognition = new SpeechRec();
    this.recognition.lang              = 'en-IN';
    this.recognition.continuous        = false;
    this.recognition.interimResults    = true;
    this.recognition.maxAlternatives   = 1;

    this.recognition.onstart  = () => this._onStart();
    this.recognition.onend    = () => this._onEnd();
    this.recognition.onerror  = (e) => this._onError(e);
    this.recognition.onresult = (e) => this._onResult(e);
  }

  _loadVoices() {
    const loadVoices = () => { this.voices = this.synthesis?.getVoices() || []; };
    loadVoices();
    if (this.synthesis) this.synthesis.onvoiceschanged = loadVoices;
  }

  _buildUI() {
    // Create floating voice widget
    const existing = document.getElementById('voice-widget');
    if (existing) existing.remove();

    this.widget = document.createElement('div');
    this.widget.id = 'voice-widget';
    this.widget.className = 'voice-widget';
    this.widget.innerHTML = `
      <div class="voice-expanded" id="voice-expanded">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
          <span style="font-size:12px;font-weight:700;color:var(--ink-2)" id="voice-status">Ready</span>
          <button onclick="voiceAssistant.stop()" style="font-size:11px;color:var(--ink-3);background:var(--bg-subtle);border:none;padding:3px 8px;border-radius:var(--r-full);cursor:pointer">Stop</button>
        </div>
        <div class="voice-waveform" id="voice-waveform">
          <div class="voice-bar" style="background:var(--accent);height:4px;animation-play-state:paused"></div>
          <div class="voice-bar" style="background:var(--accent);height:4px;animation-play-state:paused"></div>
          <div class="voice-bar" style="background:var(--accent);height:4px;animation-play-state:paused"></div>
          <div class="voice-bar" style="background:var(--accent);height:4px;animation-play-state:paused"></div>
          <div class="voice-bar" style="background:var(--accent);height:4px;animation-play-state:paused"></div>
        </div>
        <div class="voice-transcript" id="voice-transcript">Tap the mic to speak...</div>
      </div>
      <button class="voice-btn" id="voice-main-btn" aria-label="Voice input" title="Voice input (Ctrl+Shift+V)">
        🎤
      </button>`;
    document.body.appendChild(this.widget);

    this.btn       = document.getElementById('voice-main-btn');
    this.expanded  = document.getElementById('voice-expanded');
    this.statusEl  = document.getElementById('voice-status');
    this.waveform  = document.getElementById('voice-waveform');
    this.transcriptEl = document.getElementById('voice-transcript');

    this.btn.addEventListener('click', () => this.toggle());
  }

  _bindShortcuts() {
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'V') { e.preventDefault(); this.toggle(); }
      if (e.key === 'Escape' && this.isListening) this.stop();
    });
  }

  toggle() {
    if (this.isListening) this.stop();
    else this.start();
  }

  start() {
    if (!this.recognition) { Toast.warning('Voice input not supported in this browser'); return; }
    if (this.isListening) return;

    this.synthesis?.cancel(); // stop any TTS
    this.isListening = true;
    this.transcript  = '';

    this.btn.classList.add('listening');
    this.btn.textContent = '🔴';
    this.expanded.classList.add('show');
    this._setStatus('Listening...');
    this._animateWave(true);
    this.transcriptEl.textContent = '';

    // Auto-stop after 10s silence
    this.silenceTimer = setTimeout(() => this.stop(), 10000);

    try { this.recognition.start(); }
    catch (e) { this._onError({ error: e.message }); }
  }

  stop() {
    if (!this.isListening) return;
    clearTimeout(this.silenceTimer);
    this.recognition?.stop();
  }

  _onStart() {
    this._setStatus('Listening...');
  }

  _onEnd() {
    this.isListening = false;
    this.btn.classList.remove('listening');
    this.btn.textContent = '🎤';
    this._animateWave(false);

    if (this.transcript.trim()) {
      this._setStatus('Processing...');
      this.transcriptEl.textContent = `"${this.transcript}"`;
      this.onTranscript(this.transcript.trim());
      setTimeout(() => this.expanded.classList.remove('show'), 3000);
    } else {
      this._setStatus('No speech detected');
      setTimeout(() => this.expanded.classList.remove('show'), 2000);
    }
  }

  _onResult(e) {
    clearTimeout(this.silenceTimer);
    let interim = '', final = '';
    for (let r of e.results) {
      if (r.isFinal) final += r[0].transcript;
      else interim += r[0].transcript;
    }
    this.transcript = final || interim;
    this.transcriptEl.textContent = this.transcript;

    // Reset silence timer
    this.silenceTimer = setTimeout(() => this.stop(), 2000);
  }

  _onError(e) {
    this.isListening = false;
    this.btn.classList.remove('listening');
    this.btn.textContent = '🎤';
    this._animateWave(false);
    this._setStatus('Error: ' + (e.error || 'unknown'));
    setTimeout(() => this.expanded.classList.remove('show'), 2000);
    Toast.error('Voice input error: ' + (e.error || 'unknown'));
  }

  speak(text, onDone) {
    if (!this.synthesis || !text) { onDone?.(); return; }
    this.synthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.voice   = this._getBestVoice();
    utterance.rate    = 0.95;
    utterance.pitch   = 1.0;
    utterance.volume  = 0.9;
    utterance.lang    = 'en-IN';

    this.isSpeaking = true;
    this._setStatus('Speaking...');
    this._animateWave(true);
    this.expanded.classList.add('show');
    this.transcriptEl.textContent = text.slice(0, 80) + (text.length > 80 ? '...' : '');

    utterance.onend = () => {
      this.isSpeaking = false;
      this._animateWave(false);
      this._setStatus('Done');
      setTimeout(() => this.expanded.classList.remove('show'), 1500);
      onDone?.();
    };
    utterance.onerror = () => { this.isSpeaking = false; onDone?.(); };
    this.synthesis.speak(utterance);
  }

  _getBestVoice() {
    return this.voices.find(v => v.lang === 'en-IN') ||
           this.voices.find(v => v.lang === 'en-GB') ||
           this.voices.find(v => v.lang.startsWith('en')) ||
           this.voices[0] || null;
  }

  _setStatus(text) { if (this.statusEl) this.statusEl.textContent = text; }

  _animateWave(active) {
    if (!this.waveform) return;
    this.waveform.querySelectorAll('.voice-bar').forEach(bar => {
      bar.style.animationPlayState = active ? 'running' : 'paused';
      bar.style.height = active ? '' : '4px';
    });
  }
}

// ── INLINE MIC BUTTON (for chat inputs) ──────────────────────
function createInlineMicBtn(onTranscript) {
  const btn = document.createElement('button');
  btn.className = 'btn btn-icon btn-secondary';
  btn.title = 'Voice input (Ctrl+Shift+V)';
  btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
    <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
    <line x1="12" y1="19" x2="12" y2="23"/>
    <line x1="8"  y1="23" x2="16" y2="23"/>
  </svg>`;

  let isListening = false;
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) { btn.disabled = true; btn.title = 'Not supported'; return btn; }

  const rec = new SpeechRec();
  rec.lang = 'en-IN';
  rec.interimResults = false;

  rec.onresult = (e) => {
    const text = e.results[0][0].transcript;
    onTranscript(text);
    isListening = false;
    btn.style.color = '';
  };
  rec.onend = () => { isListening = false; btn.style.color = ''; };
  rec.onerror = () => { isListening = false; btn.style.color = ''; };

  btn.addEventListener('click', () => {
    if (isListening) { rec.stop(); return; }
    isListening = true;
    btn.style.color = 'var(--red)';
    rec.start();
  });
  return btn;
}

window.AreaPulseVoiceAssistant = AreaPulseVoiceAssistant;
window.createInlineMicBtn = createInlineMicBtn;
