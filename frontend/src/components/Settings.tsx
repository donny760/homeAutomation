'use client';

import { useState, useEffect } from 'react';

interface ConnectorInterval {
  key: string;
  label: string;
  unit: string;
}

interface Connector {
  key: string;
  label: string;
  type: string;
  enabled_key?: string;
  intervals: ConnectorInterval[];
}

interface SettingsData {
  settings: Record<string, string>;
  connectors: Connector[];
}

const TYPE_LABELS: Record<string, string> = {
  continuous: 'Continuous Poller',
  'on-demand': 'On-demand',
  websocket: 'Websocket (event-driven)',
  scheduled: 'Scheduled Tasks',
  frontend: 'Browser Intervals',
  configurable: 'Configuration',
};

function secondsToBestUnit(secs: number): { value: number; unit: string } {
  secs = Number(secs);
  if (secs >= 3600 && secs % 3600 === 0) return { value: secs / 3600, unit: 'hr' };
  if (secs >= 60 && secs % 60 === 0) return { value: secs / 60, unit: 'min' };
  return { value: secs, unit: 's' };
}

function msToBestUnit(ms: number): { value: number; unit: string } {
  ms = Number(ms);
  if (ms >= 3600000 && ms % 3600000 === 0) return { value: ms / 3600000, unit: 'hr' };
  if (ms >= 60000 && ms % 60000 === 0) return { value: ms / 60000, unit: 'min' };
  if (ms >= 1000 && ms % 1000 === 0) return { value: ms / 1000, unit: 's' };
  return { value: ms, unit: 'ms' };
}

function toStorageValue(displayVal: number, displayUnit: string, storageUnit: string): number {
  const v = Number(displayVal);
  if (storageUnit === 's') {
    if (displayUnit === 'hr') return v * 3600;
    if (displayUnit === 'min') return v * 60;
    return v;
  }
  if (storageUnit === 'ms') {
    if (displayUnit === 'hr') return v * 3600000;
    if (displayUnit === 'min') return v * 60000;
    if (displayUnit === 's') return v * 1000;
    return v;
  }
  return v;
}

const UNIT_OPTIONS: Record<string, string[] | null> = {
  s: ['s', 'min', 'hr'],
  ms: ['ms', 's', 'min', 'hr'],
  days: null,
  months: null,
  url: null,
  text: null,
  date: null,
};

interface SettingsProps {
  isActive: boolean;
}

export default function Settings({ isActive }: SettingsProps) {
  const [data, setData] = useState<SettingsData | null>(null);
  const [status, setStatus] = useState('');
  const [alexaStatus, setAlexaStatus] = useState<string>('');
  const [alexaNeedsOtp, setAlexaNeedsOtp] = useState(false);
  const [alexaOtp, setAlexaOtp] = useState('');
  const [alexaBusy, setAlexaBusy] = useState(false);

  async function alexaAuthenticate() {
    setAlexaBusy(true);
    setAlexaStatus('Authenticating...');
    try {
      await saveCard('alexa'); // persist email/password/url first
      const res = await fetch('/alexa/auth', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (body.needs_otp) {
        setAlexaNeedsOtp(true);
        setAlexaStatus('OTP required — check your Alexa app / authenticator.');
      } else if (body.needs_captcha) {
        setAlexaStatus(`Captcha required (not auto-supported). Image: ${body.captcha_url || 'n/a'}`);
      } else if (body.ok) {
        setAlexaNeedsOtp(false);
        setAlexaStatus('Signed in. Hit rediscover in the Switches drawer.');
      } else {
        const msg = body.error || body.hint || JSON.stringify(body.status || {});
        setAlexaStatus(`Login did not complete. ${msg}`);
      }
    } catch (e) {
      setAlexaStatus(`Error: ${String(e)}`);
    } finally {
      setAlexaBusy(false);
    }
  }

  async function alexaSubmitOtp() {
    if (!alexaOtp.trim()) return;
    setAlexaBusy(true);
    setAlexaStatus('Submitting OTP...');
    try {
      const res = await fetch('/alexa/otp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ otp: alexaOtp.trim() }),
      });
      const body = await res.json().catch(() => ({}));
      if (body.ok) {
        setAlexaNeedsOtp(false);
        setAlexaOtp('');
        setAlexaStatus('Signed in. Hit rediscover in the Switches drawer.');
      } else if (body.needs_otp) {
        setAlexaStatus('OTP rejected — try again.');
      } else {
        setAlexaStatus(`Error: ${body.error || 'unknown'}`);
      }
    } catch (e) {
      setAlexaStatus(`Error: ${String(e)}`);
    } finally {
      setAlexaBusy(false);
    }
  }

  useEffect(() => {
    if (isActive) refresh();
  }, [isActive]);

  async function refresh() {
    try {
      const d = await fetch('/api/settings').then((r) => r.json());
      setData(d);
    } catch (e) {
      console.warn('Settings:', e);
    }
  }

  function showStatus(msg: string) {
    setStatus(msg);
    setTimeout(() => setStatus(''), 2000);
  }

  async function saveToggle(key: string, checked: boolean) {
    try {
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: checked ? '1' : '0' }),
      });
      showStatus('Saved');
      refresh();
    } catch (e) {
      console.warn('Settings toggle:', e);
    }
  }

  async function saveCard(connKey: string) {
    const card = document.querySelector(`[data-connector="${connKey}"]`);
    if (!card) return;
    const inputs = card.querySelectorAll('input[data-key]:not([type="checkbox"])');
    const selects = card.querySelectorAll('select[data-key]');
    const updates: Record<string, string> = {};
    inputs.forEach((inp) => {
      const input = inp as HTMLInputElement;
      const key = input.dataset.key!;
      const storageUnit = input.dataset.storageUnit || 's';
      if (storageUnit === 'url' || storageUnit === 'text' || storageUnit === 'date' || storageUnit === 'password') {
        updates[key] = input.value;
      } else {
        const unitSelect = card.querySelector(`select[data-for="${key}"]`) as HTMLSelectElement | null;
        const displayUnit = unitSelect ? unitSelect.value : storageUnit;
        updates[key] = String(toStorageValue(Number(input.value), displayUnit, storageUnit));
      }
    });
    selects.forEach((sel) => {
      const el = sel as HTMLSelectElement;
      updates[el.dataset.key!] = el.value;
    });
    try {
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      });
      showStatus('Saved');
    } catch (e) {
      console.warn('Settings save:', e);
    }
  }

  if (!data) {
    return (
      <div id="page-settings" className="page active">
        <div className="settings-toolbar">
          <div className="page-title">Settings</div>
        </div>
        <div className="settings-grid">
          <div className="costs-empty">Loading...</div>
        </div>
      </div>
    );
  }

  const settings = data.settings || {};
  const connectors = data.connectors || [];

  return (
    <div id="page-settings" className="page active">
      <div className="settings-toolbar">
        <div className="page-title">Settings</div>
        <span className="settings-status">{status}</span>
      </div>
      <div className="settings-grid" id="settings-grid">
        {connectors.map((conn) => {
          const hasToggle = !!conn.enabled_key;
          const enabled = hasToggle ? settings[conn.enabled_key!] === '1' : true;
          const dotCls = enabled ? 'on' : 'off';

          return (
            <div key={conn.key} className="settings-card" data-connector={conn.key}>
              <div className="settings-card-header">
                <div className="settings-card-title">
                  {hasToggle && <span className={`settings-dot ${dotCls}`} />}
                  {conn.label}
                </div>
                {hasToggle && (
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={enabled}
                      data-key={conn.enabled_key}
                      onChange={(e) => saveToggle(conn.enabled_key!, e.target.checked)}
                    />
                    <span className="toggle-slider" />
                  </label>
                )}
              </div>
              <div className="settings-type">{TYPE_LABELS[conn.type] || conn.type}</div>
              {conn.intervals.map((iv) => {
                const rawVal = settings[iv.key] || '0';
                const opts = UNIT_OPTIONS[iv.unit];

                if (opts) {
                  const best = iv.unit === 'ms' ? msToBestUnit(Number(rawVal)) : secondsToBestUnit(Number(rawVal));
                  return (
                    <div key={iv.key} className="settings-interval">
                      <label>{iv.label}</label>
                      <input
                        type="number"
                        min={1}
                        data-key={iv.key}
                        data-storage-unit={iv.unit}
                        defaultValue={best.value}
                      />
                      <select className="settings-unit-select" data-for={iv.key} defaultValue={best.unit}>
                        {opts.map((u) => (
                          <option key={u} value={u}>{u}</option>
                        ))}
                      </select>
                    </div>
                  );
                }

                if (iv.unit === 'url' || iv.unit === 'text' || iv.unit === 'password') {
                  return (
                    <div key={iv.key} className="settings-interval">
                      <label>{iv.label}</label>
                      <input
                        type={iv.unit === 'password' ? 'password' : 'text'}
                        data-key={iv.key}
                        data-storage-unit={iv.unit}
                        defaultValue={settings[iv.key] || ''}
                        autoComplete={iv.unit === 'password' ? 'current-password' : 'off'}
                        style={iv.unit === 'url' ? { flex: 1, width: 'auto' } : { width: '140px' }}
                      />
                    </div>
                  );
                }

                if (iv.unit === 'date') {
                  return (
                    <div key={iv.key} className="settings-interval">
                      <label>{iv.label}</label>
                      <input
                        type="date"
                        data-key={iv.key}
                        data-storage-unit={iv.unit}
                        defaultValue={settings[iv.key] || ''}
                      />
                    </div>
                  );
                }

                if (iv.unit === 'select') {
                  const options = (iv as any).options || [];
                  return (
                    <div key={iv.key} className="settings-interval">
                      <label>{iv.label}</label>
                      <select
                        data-key={iv.key}
                        data-storage-unit={iv.unit}
                        defaultValue={settings[iv.key] || options[0] || ''}
                      >
                        {options.map((opt: string) => (
                          <option key={opt} value={opt}>{opt}</option>
                        ))}
                      </select>
                    </div>
                  );
                }

                return (
                  <div key={iv.key} className="settings-interval">
                    <label>{iv.label}</label>
                    <input
                      type="number"
                      min={1}
                      data-key={iv.key}
                      data-storage-unit={iv.unit}
                      defaultValue={rawVal}
                    />
                    <span className="settings-unit">{iv.unit}</span>
                  </div>
                );
              })}
              {conn.intervals.length > 0 && (
                <button className="settings-save-btn" onClick={() => saveCard(conn.key)}>
                  Save
                </button>
              )}
              {conn.key === 'alexa' && enabled && (
                <div className="settings-interval" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6, marginTop: 8 }}>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button
                      className="settings-save-btn"
                      onClick={alexaAuthenticate}
                      disabled={alexaBusy}
                      style={{ background: 'var(--blue)' }}
                    >
                      {alexaBusy ? 'Working...' : 'Authenticate'}
                    </button>
                  </div>
                  {alexaNeedsOtp && (
                    <div style={{ display: 'flex', gap: 6 }}>
                      <input
                        type="text"
                        placeholder="OTP"
                        value={alexaOtp}
                        onChange={(e) => setAlexaOtp(e.target.value)}
                        style={{ flex: 1 }}
                        autoComplete="one-time-code"
                      />
                      <button
                        className="settings-save-btn"
                        onClick={alexaSubmitOtp}
                        disabled={alexaBusy || !alexaOtp.trim()}
                      >
                        Submit
                      </button>
                    </div>
                  )}
                  {alexaStatus && (
                    <div style={{ fontSize: '0.72rem', color: 'var(--dim)' }}>
                      {alexaStatus}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
