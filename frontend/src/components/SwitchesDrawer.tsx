'use client';

import { useEffect, useState, useCallback, useRef } from 'react';

interface Switch {
  id: number;
  provider: 'kasa' | 'alexa' | 'pool' | 'nest' | 'tuya' | 'abode';
  external_id: string;
  kind: 'plug' | 'dimmer' | 'routine' | 'circuit' | 'thermostat' | 'alarm';
  name: string;
  room: string;
  sort_order: number;
  hidden: boolean;
  state: boolean | null;
  reachable: boolean;
  detail: {
    dimmable?: boolean;
    brightness?: number | null;
    // Thermostat fields
    mode?: string;
    available_modes?: string[];
    ambient_f?: number | null;
    humidity?: number | null;
    setpoint_heat_f?: number | null;
    setpoint_cool_f?: number | null;
    hvac_status?: string | null;
    eco_mode?: string | null;
    // Abode alarm fields
    mode_display?: string;
    connected?: boolean;
    [key: string]: unknown;
  };
}

type ThermostatMode = 'OFF' | 'HEAT' | 'COOL' | 'HEATCOOL';
const MODE_LABEL: Record<ThermostatMode, string> = {
  OFF: 'Off',
  HEAT: 'Heat',
  COOL: 'Cool',
  HEATCOOL: 'Auto',
};

interface AlarmTileProps {
  sw: Switch;
  cls: string;
  onArmHome: () => void;
  onEdit: () => void;
}

function AlarmTile({ sw, cls, onArmHome, onEdit }: AlarmTileProps) {
  const mode = (sw.detail?.mode || 'standby') as string;
  const modeDisplay = sw.detail?.mode_display || 'Unknown';
  const armedHome = mode === 'home';
  return (
    <div className={cls}>
      <span className="switch-tile-badge abode">abode</span>
      <div className="thermostat-header">
        <div>
          <span className="switch-tile-icon" style={{ marginRight: 6 }}>
            {ICON.alarm}
          </span>
          <span className="switch-tile-name" style={{ display: 'inline' }}>
            {sw.name}
          </span>
        </div>
        <div className="alarm-status">{modeDisplay}</div>
      </div>
      <button
        className={`alarm-btn${armedHome ? ' armed' : ''}`}
        disabled={!sw.reachable || armedHome}
        onClick={onArmHome}
      >
        {armedHome ? 'Armed' : 'Arm Home'}
      </button>
      <button className="switch-tile-gear" onClick={onEdit} aria-label="Edit">
        &#9881;
      </button>
    </div>
  );
}

interface ThermostatTileProps {
  sw: Switch;
  cls: string;
  localSetpoint?: { heat?: number; cool?: number; single?: number };
  onMode: (mode: ThermostatMode) => void;
  onSet: (which: 'single' | 'heat' | 'cool', value: number) => void;
  onEdit: () => void;
}

const TEMP_MIN = 50;
const TEMP_MAX = 90;

const SECTION_ACCENTS = [
  'accent-blue',
  'accent-green',
  'accent-amber',
  'accent-purple',
  'accent-pool',
  'accent-nest',
];
function sectionAccent(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return SECTION_ACCENTS[Math.abs(h) % SECTION_ACCENTS.length];
}

interface SetpointSliderProps {
  label: string;
  value: number | null;
  reachable: boolean;
  onChange: (value: number) => void;
}

function SetpointSlider({ label, value, reachable, onChange }: SetpointSliderProps) {
  const disabled = !reachable || value == null;
  const display = value != null ? Math.round(value) : null;
  const pct =
    display != null
      ? ((display - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)) * 100
      : 50;
  return (
    <div className="temp-slider-row">
      <span className="temp-slider-label">{label}</span>
      <input
        type="range"
        className="temp-slider"
        min={TEMP_MIN}
        max={TEMP_MAX}
        step={1}
        value={display ?? 70}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ ['--tpct' as string]: `${pct}%` } as React.CSSProperties}
      />
      <span className="temp-slider-val">
        {display != null ? `${display}\u00B0F` : '--'}
      </span>
    </div>
  );
}

function ThermostatTile({ sw, cls, localSetpoint, onMode, onSet, onEdit }: ThermostatTileProps) {
  const mode = ((sw.detail?.mode || 'OFF') as string).toUpperCase() as ThermostatMode;
  const avail = (sw.detail?.available_modes || [
    'OFF',
    'HEAT',
    'COOL',
    'HEATCOOL',
  ]) as ThermostatMode[];
  const ambient = sw.detail?.ambient_f;
  const humidity = sw.detail?.humidity;
  const hvac = (sw.detail?.hvac_status || 'OFF') as string;
  const heatF = localSetpoint?.heat ?? (sw.detail?.setpoint_heat_f ?? null);
  const coolF = localSetpoint?.cool ?? (sw.detail?.setpoint_cool_f ?? null);

  // "single" setpoint view for HEAT or COOL modes
  let singleVal: number | null = null;
  let singleKind: 'heat' | 'cool' | null = null;
  if (mode === 'HEAT') {
    singleVal = localSetpoint?.single ?? heatF;
    singleKind = 'heat';
  } else if (mode === 'COOL') {
    singleVal = localSetpoint?.single ?? coolF;
    singleKind = 'cool';
  }

  return (
    <div className={cls}>
      <span className="switch-tile-badge nest">nest</span>
      <div className="thermostat-header">
        <div>
          <span className="switch-tile-icon" style={{ marginRight: 6 }}>
            {ICON.thermostat}
          </span>
          <span className="switch-tile-name" style={{ display: 'inline' }}>
            {sw.name}
          </span>
        </div>
        <div>
          <span className="thermostat-ambient">
            {ambient != null ? `${ambient}\u00B0F` : '--'}
          </span>
          {humidity != null && (
            <span className="thermostat-ambient-sub">{humidity}% RH</span>
          )}
        </div>
      </div>
      <div className="mode-segment">
        {(['OFF', 'HEAT', 'COOL', 'HEATCOOL'] as ThermostatMode[]).map((m) => (
          <button
            key={m}
            className={mode === m ? 'active' : ''}
            disabled={!sw.reachable || !avail.includes(m)}
            onClick={() => onMode(m)}
          >
            {MODE_LABEL[m]}
          </button>
        ))}
      </div>
      {mode === 'HEATCOOL' ? (
        <>
          <SetpointSlider
            label="Heat"
            value={heatF}
            reachable={sw.reachable}
            onChange={(v) => onSet('heat', v)}
          />
          <SetpointSlider
            label="Cool"
            value={coolF}
            reachable={sw.reachable}
            onChange={(v) => onSet('cool', v)}
          />
        </>
      ) : mode === 'HEAT' || mode === 'COOL' ? (
        <>
          <SetpointSlider
            label="Target"
            value={singleVal}
            reachable={sw.reachable}
            onChange={(v) => onSet('single', v)}
          />
          <span
            className={`thermostat-hvac${hvac !== 'OFF' ? ' active' : ''}`}
            style={{ alignSelf: 'flex-end', marginTop: -2 }}
          >
            {hvac}
          </span>
          {singleKind && ''}
        </>
      ) : (
        <div className="thermostat-hvac">System is off</div>
      )}
      <button
        className="switch-tile-gear"
        onClick={onEdit}
        aria-label="Edit"
      >
        &#9881;
      </button>
    </div>
  );
}

interface DrawerProps {
  open: boolean;
  onClose: () => void;
}

const ICON: Record<Switch['kind'], string> = {
  plug:       '\u{1F50C}',  // 🔌
  dimmer:     '\u{1F4A1}',  // 💡
  routine:    '\u25B6',     // ▶
  circuit:    '\u{1F4A7}',  // 💧
  thermostat: '\u{1F321}',  // 🌡
  alarm:      '\u{1F6E1}',  // 🛡
};

// Pool circuits get per-circuit icons since they're all kind='circuit'
// but semantically very different.
const POOL_ICON: Record<string, string> = {
  '500':  '\u{1F6C1}',   // 🛁 Spa
  '501':  '\u{1F4A1}',   // 💡 Pool Light
  '502':  '\u{1F4A1}',   // 💡 Water Light
  '503':  '\u{1F4A1}',   // 💡 Spa Light
  '504':  '\u{1F30A}',   // 🌊 Waterfall
  '505':  '\u{1F3CA}',   // 🏊 Pool
  '506':  '\u2699',      // ⚙ Edge Pump
  '507':  '\u{1F4A6}',   // 💦 Spillway
  '508':  '\u{1F9F9}',   // 🧹 Cleaner
  'feat1':'\u2728',      // ✨ Feature 1
};

function iconFor(sw: Switch): string {
  if (sw.provider === 'pool') {
    return POOL_ICON[sw.external_id] ?? ICON.circuit;
  }
  return ICON[sw.kind] ?? ICON.plug;
}

export default function SwitchesDrawer({ open, onClose }: DrawerProps) {
  const [switches, setSwitches] = useState<Switch[]>([]);
  const [loading, setLoading] = useState(false);
  const [rediscovering, setRediscovering] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState('');
  const [editRoom, setEditRoom] = useState('');
  const [editHidden, setEditHidden] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  // Optimistic brightness overrides so the slider feels instant during drag
  const [localBrightness, setLocalBrightness] = useState<Record<number, number>>({});
  // Optimistic thermostat setpoint overrides so stepper feels instant
  const [localSetpoint, setLocalSetpoint] = useState<
    Record<number, { heat?: number; cool?: number; single?: number }>
  >({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const brightnessTimers = useRef<Record<number, ReturnType<typeof setTimeout>>>({});
  const setpointTimers = useRef<Record<number, ReturnType<typeof setTimeout>>>({});

  const load = useCallback(async () => {
    try {
      const data: Switch[] = await fetch('/api/switches').then((r) => r.json());
      setSwitches(data);
    } catch (e) {
      console.warn('Switches load:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      setLocalBrightness({});
      return;
    }
    setLoading(true);
    load();
    pollRef.current = setInterval(load, 10_000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  async function toggle(id: number) {
    try {
      const res = await fetch('/api/switches/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.warn('Toggle failed:', err);
      }
      load();
    } catch (e) {
      console.warn('Toggle error:', e);
    }
  }

  function handleBrightness(id: number, value: number) {
    setLocalBrightness((prev) => ({ ...prev, [id]: value }));
    const pending = brightnessTimers.current[id];
    if (pending) clearTimeout(pending);
    brightnessTimers.current[id] = setTimeout(async () => {
      try {
        await fetch('/api/switches/brightness', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, brightness: value }),
        });
      } catch (e) {
        console.warn('Brightness error:', e);
      } finally {
        delete brightnessTimers.current[id];
        load();
      }
    }, 300);
  }

  async function armAlarmHome(id: number) {
    try {
      const res = await fetch('/api/switches/alarm/arm-home', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.warn('Arm home failed:', err);
      }
      load();
    } catch (e) {
      console.warn('Arm home error:', e);
    }
  }

  async function setThermostatMode(id: number, mode: ThermostatMode) {
    try {
      const res = await fetch('/api/switches/thermostat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, mode }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.warn('Mode change failed:', err);
      }
      setLocalSetpoint((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      load();
    } catch (e) {
      console.warn('Thermostat mode error:', e);
    }
  }

  // Per-(id, which) target for debounced send, tracked outside React state
  // so the timer callback reads the latest value after rapid clicks.
  const setpointTargets = useRef<Record<string, number>>({});

  function setSetpoint(
    id: number,
    which: 'single' | 'heat' | 'cool',
    value: number
  ) {
    const newVal = Math.round(value);
    setLocalSetpoint((prev) => {
      const existing = prev[id] || {};
      return { ...prev, [id]: { ...existing, [which]: newVal } };
    });
    setpointTargets.current[`${id}:${which}`] = newVal;

    const pending = setpointTimers.current[id];
    if (pending) clearTimeout(pending);
    setpointTimers.current[id] = setTimeout(async () => {
      const body: Record<string, unknown> = { id };
      const s = setpointTargets.current[`${id}:single`];
      const h = setpointTargets.current[`${id}:heat`];
      const c = setpointTargets.current[`${id}:cool`];
      if (s !== undefined) body.setpoint_f = s;
      if (h !== undefined) body.setpoint_heat_f = h;
      if (c !== undefined) body.setpoint_cool_f = c;
      delete setpointTargets.current[`${id}:single`];
      delete setpointTargets.current[`${id}:heat`];
      delete setpointTargets.current[`${id}:cool`];
      try {
        await fetch('/api/switches/thermostat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } catch (e) {
        console.warn('Setpoint error:', e);
      } finally {
        delete setpointTimers.current[id];
        // Clear optimistic state for this id so the server-confirmed value shows
        setLocalSetpoint((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        load();
      }
    }, 500);
  }

  async function rediscover() {
    setRediscovering(true);
    try {
      await fetch('/api/switches/rediscover', { method: 'POST' });
      await load();
    } catch (e) {
      console.warn('Rediscover error:', e);
    } finally {
      setRediscovering(false);
    }
  }

  function startEdit(sw: Switch) {
    setEditingId(sw.id);
    setEditName(sw.name);
    setEditRoom(sw.room);
    setEditHidden(sw.hidden);
  }

  async function saveEdit() {
    if (editingId == null) return;
    try {
      await fetch(`/api/switches/${editingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editName.trim(),
          room: editRoom.trim(),
          hidden: editHidden,
        }),
      });
      setEditingId(null);
      load();
    } catch (e) {
      console.warn('Save edit:', e);
    }
  }

  function statusText(sw: Switch): string {
    if (!sw.reachable) return 'offline';
    if (sw.kind === 'routine') return 'tap to run';
    if (sw.state == null) return '--';
    if (sw.kind === 'dimmer' && sw.state === true) {
      const b = localBrightness[sw.id] ?? sw.detail?.brightness;
      if (typeof b === 'number') return `${b}%`;
    }
    return sw.state ? 'On' : 'Off';
  }

  const visible = switches.filter((s) => showHidden || !s.hidden);
  const byRoom = new Map<string, Switch[]>();
  for (const s of visible) {
    const key = s.room || 'Unassigned';
    if (!byRoom.has(key)) byRoom.set(key, []);
    byRoom.get(key)!.push(s);
  }
  byRoom.forEach((arr) => {
    arr.sort((a: Switch, b: Switch) =>
      a.name.localeCompare(b.name, undefined, { numeric: true })
    );
  });
  const roomOrder = Array.from(byRoom.keys()).sort((a, b) => {
    if (a === 'Unassigned') return 1;
    if (b === 'Unassigned') return -1;
    return a.localeCompare(b);
  });

  return (
    <>
      <div
        className={`drawer-backdrop${open ? ' open' : ''}`}
        onClick={onClose}
      />
      <div className={`drawer${open ? ' open' : ''}`} role="dialog" aria-label="Home Control">
        <div className="drawer-header">
          <div className="drawer-title">
            <span className="drawer-title-icon">&#x1F3E0;</span>
            Home Control
          </div>
          <div className="drawer-actions">
            <button
              className="btn-icon"
              onClick={() => setShowHidden((v) => !v)}
              title={showHidden ? 'Hide hidden switches' : 'Show hidden switches'}
              style={showHidden ? { color: 'var(--amber)' } : undefined}
            >
              {showHidden ? '\u{1F441}' : '\u{1F441}\u{FE0E}'}
            </button>
            <button
              className="btn-icon"
              onClick={rediscover}
              disabled={rediscovering}
              title="Rediscover devices"
            >
              {rediscovering ? '...' : '\u21BB'}
            </button>
            <button className="drawer-close" onClick={onClose} aria-label="Close">
              &times;
            </button>
          </div>
        </div>
        <div className="drawer-body">
          {loading && switches.length === 0 ? (
            <div className="drawer-empty">Loading&hellip;</div>
          ) : visible.length === 0 ? (
            <div className="drawer-empty">
              No switches yet.
              <br />
              Enable a provider in Settings, then tap &#x21BB; above to discover.
            </div>
          ) : (
            roomOrder.map((room) => (
              <div key={room} className={`drawer-section ${sectionAccent(room)}`}>
                <div className="drawer-section-header">
                  <div className="drawer-section-title">{room}</div>
                </div>
                <div className="switch-grid">
                  {byRoom.get(room)!.map((sw) => {
                    const on = sw.state === true;
                    const isDimmer = sw.kind === 'dimmer' && !!sw.detail?.dimmable;
                    const isThermostat = sw.kind === 'thermostat';
                    const brightness =
                      localBrightness[sw.id] ??
                      (typeof sw.detail?.brightness === 'number'
                        ? sw.detail.brightness
                        : 100);
                    const cls = [
                      'switch-tile',
                      isThermostat ? 'thermostat' : '',
                      sw.kind === 'alarm' ? 'alarm' : '',
                      on ? 'on' : '',
                      sw.reachable ? '' : 'unreachable',
                      sw.hidden ? 'is-hidden' : '',
                    ]
                      .filter(Boolean)
                      .join(' ');
                    if (isThermostat && editingId !== sw.id) {
                      return (
                        <ThermostatTile
                          key={sw.id}
                          sw={sw}
                          cls={cls}
                          localSetpoint={localSetpoint[sw.id]}
                          onMode={(m) => setThermostatMode(sw.id, m)}
                          onSet={(which, value) => setSetpoint(sw.id, which, value)}
                          onEdit={() => startEdit(sw)}
                        />
                      );
                    }
                    if (sw.kind === 'alarm' && editingId !== sw.id) {
                      return (
                        <AlarmTile
                          key={sw.id}
                          sw={sw}
                          cls={cls}
                          onArmHome={() => armAlarmHome(sw.id)}
                          onEdit={() => startEdit(sw)}
                        />
                      );
                    }
                    return editingId === sw.id ? (
                      <div key={sw.id} className="switch-tile-edit">
                        <label>
                          Name
                          <input
                            type="text"
                            value={editName}
                            onChange={(e) => setEditName(e.target.value)}
                          />
                        </label>
                        <label>
                          Room
                          <input
                            type="text"
                            value={editRoom}
                            onChange={(e) => setEditRoom(e.target.value)}
                            placeholder="(leave blank for Unassigned)"
                          />
                        </label>
                        <label style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                          <input
                            type="checkbox"
                            checked={editHidden}
                            onChange={(e) => setEditHidden(e.target.checked)}
                          />
                          Hide from panel
                        </label>
                        <div className="switch-tile-edit-row">
                          <button
                            className="btn-icon"
                            onClick={() => setEditingId(null)}
                          >
                            Cancel
                          </button>
                          <button className="btn-icon" onClick={saveEdit}>
                            Save
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div
                        key={sw.id}
                        className={cls}
                        onClick={() => sw.reachable && toggle(sw.id)}
                        role="button"
                        aria-pressed={on}
                        style={
                          isDimmer
                            ? ({ ['--b' as string]: `${brightness}%` } as React.CSSProperties)
                            : undefined
                        }
                      >
                        <span className={`switch-tile-badge ${sw.provider}`}>
                          {sw.provider}
                        </span>
                        <div className="switch-tile-icon">{iconFor(sw)}</div>
                        <div className="switch-tile-name">{sw.name}</div>
                        <div className="switch-tile-status">{statusText(sw)}</div>
                        {isDimmer && (
                          <input
                            type="range"
                            className="switch-tile-slider"
                            min={0}
                            max={100}
                            value={brightness}
                            disabled={!sw.reachable}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) =>
                              handleBrightness(sw.id, Number(e.target.value))
                            }
                          />
                        )}
                        <button
                          className="switch-tile-gear"
                          onClick={(e) => {
                            e.stopPropagation();
                            startEdit(sw);
                          }}
                          aria-label="Edit"
                        >
                          &#9881;
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
