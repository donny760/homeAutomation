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
    const updates: Record<string, string> = {};
    inputs.forEach((inp) => {
      const input = inp as HTMLInputElement;
      const key = input.dataset.key!;
      const storageUnit = input.dataset.storageUnit || 's';
      if (storageUnit === 'url' || storageUnit === 'text' || storageUnit === 'date') {
        updates[key] = input.value;
      } else {
        const unitSelect = card.querySelector(`select[data-for="${key}"]`) as HTMLSelectElement | null;
        const displayUnit = unitSelect ? unitSelect.value : storageUnit;
        updates[key] = String(toStorageValue(Number(input.value), displayUnit, storageUnit));
      }
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

                if (iv.unit === 'url' || iv.unit === 'text') {
                  return (
                    <div key={iv.key} className="settings-interval">
                      <label>{iv.label}</label>
                      <input
                        type="text"
                        data-key={iv.key}
                        data-storage-unit={iv.unit}
                        defaultValue={settings[iv.key] || ''}
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
            </div>
          );
        })}
      </div>
    </div>
  );
}
