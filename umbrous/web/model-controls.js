// Capability-driven provider controls. Stores only model choices, never credentials.
(() => {
  'use strict';
  const KEY = 'umbrous.model-controls.v1';
  function isCloud(model) { return !!model && (['codex', 'claude', 'grok'].includes(model.provider) || /^(codex|claude|grok)\//.test(model.name)); }
  function loginUrl(raw) {
    if (typeof raw !== 'string' || /[\s\\]/.test(raw)) return null;
    let url;
    try { url = new URL(raw); } catch (_) { return null; }
    if (url.protocol !== 'https:' || !['auth.openai.com', 'chatgpt.com'].includes(url.hostname)
        || url.username || url.password || (url.port && url.port !== '443')) return null;
    return url.href;
  }
  class Preferences {
    constructor(storage) {
      if (arguments.length === 0) { try { storage = globalThis.localStorage; } catch (_) { storage = null; } }
      this.storage = storage;
      this.selected = null;
      this.choices = Object.create(null);
      try {
        const raw = storage.getItem(KEY);
        const data = raw && raw.length <= 65536 ? JSON.parse(raw) : null;
        if (data && typeof data.selected === 'string') this.selected = data.selected;
        if (data && data.choices && typeof data.choices === 'object' && !Array.isArray(data.choices)) {
          for (const [name, value] of Object.entries(data.choices)) {
            if (value && typeof value.speed === 'string' && (value.reasoning_effort == null || typeof value.reasoning_effort === 'string')) {
              this.choices[name] = { speed: value.speed, reasoning_effort: value.reasoning_effort || null };
            }
          }
        }
      } catch (_) { /* Storage may be unavailable; controls still work in memory. */ }
    }
    save() {
      try { this.storage.setItem(KEY, JSON.stringify({ selected: this.selected, choices: this.choices })); } catch (_) {}
    }
    choose(name) { this.selected = name; this.save(); }
    forModel(model) {
      if (!model || !isCloud(model)) return {};
      const saved = this.choices[model.name];
      if (saved) return { ...saved };
      const effort = model.default_reasoning_effort;
      return { reasoning_effort: (model.reasoning_efforts || []).includes(effort) ? effort : null, speed: 'standard' };
    }
    supports(model) {
      if (!isCloud(model)) return true;
      const current = this.forModel(model);
      return (!current.reasoning_effort || (model.reasoning_efforts || []).includes(current.reasoning_effort))
        && (model.speeds || []).includes(current.speed);
    }
    change(model, field, value) {
      if (!isCloud(model)) return false;
      const allowed = field === 'reasoning_effort' ? model.reasoning_efforts : field === 'speed' ? model.speeds : [];
      if (!(allowed || []).includes(value)) return false;
      this.choices[model.name] = { ...this.forModel(model), [field]: value };
      this.save();
      return true;
    }
  }
  const api = Object.freeze({ isCloud, loginUrl, Preferences });
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.UmbrousModelControls = api;
})();
