'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import NetworkPinModal from './NetworkPinModal';

interface Device {
  mac: string;
  friendly_name: string;
  notes: string;
  hidden: boolean;
  vendor: string | null;
  last_ip: string | null;
  last_hostname: string | null;
  nbns_name: string | null;
  last_ap: string | null;
  last_signal: number | null;
  last_iface: string | null;
  first_seen: number | null;
  last_seen: number | null;
  online: boolean;
  seen_on: string[];
}

interface DevicesResp {
  devices: Device[];
  total: number;
  aps: string[];
  last_poll_ts: number;
  last_poll: { devices_seen?: number; aps_polled?: number; aps_skipped_quarantined?: number; elapsed_ms?: number; errors?: number };
  enabled: boolean;
  quarantined_aps: { name: string; until: number }[];
}

interface NetworkDevicesProps {
  isActive: boolean;
}

function relativeTime(ts: number | null): string {
  if (!ts) return '—';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function shortDate(ts: number | null): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  // Show "Jan 5" if same year, "Jan 5 '24" if not.
  const now = new Date();
  const month = d.toLocaleString('en-US', { month: 'short' });
  if (d.getFullYear() === now.getFullYear()) {
    return `${month} ${d.getDate()}`;
  }
  return `${month} ${d.getDate()} '${String(d.getFullYear()).slice(-2)}`;
}

function radioLabel(iface: string | null): string {
  if (iface === 'wl0') return '2.4GHz';
  if (iface === 'wl1') return '5GHz';
  return iface || '';
}

// Tier the row's offline-ness so we can fade old entries and gate the
// remove button. Thresholds match the server-side guard (90d minimum
// before DELETE will succeed).
const STALE_WARM_DAYS = 7;
const STALE_COLD_DAYS = 90;
function stalenessTier(lastSeen: number | null, online: boolean):
    'live' | 'warm' | 'cold' {
  if (online || !lastSeen) return 'live';
  const ageDays = (Date.now() / 1000 - lastSeen) / 86400;
  if (ageDays >= STALE_COLD_DAYS) return 'cold';
  if (ageDays >= STALE_WARM_DAYS) return 'warm';
  return 'live';
}

export default function NetworkDevices({ isActive }: NetworkDevicesProps) {
  const [data, setData] = useState<DevicesResp | null>(null);
  const [search, setSearch] = useState('');
  const [unnamedOnly, setUnnamedOnly] = useState(false);
  const [apFilter, setApFilter] = useState('');
  const [connFilter, setConnFilter] = useState<'' | 'wireless' | 'wired'>('');
  const [editingMac, setEditingMac] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [pinDevice, setPinDevice] = useState<Device | null>(null);
  const [confirmRemoveMac, setConfirmRemoveMac] = useState<string | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch('/api/network/devices');
      if (r.ok) setData(await r.json());
    } catch (e) {
      console.warn('network fetch:', e);
    }
  }, []);

  useEffect(() => {
    if (!isActive) return;
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, [isActive, refresh]);

  useEffect(() => {
    if (editingMac && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingMac]);

  function startEdit(d: Device) {
    setEditingMac(d.mac);
    setEditValue(d.friendly_name);
  }

  async function saveEdit() {
    if (!editingMac) return;
    setBusy(true);
    try {
      await fetch(`/api/network/devices/${editingMac}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ friendly_name: editValue }),
      });
      setEditingMac(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  function cancelEdit() {
    setEditingMac(null);
    setEditValue('');
  }

  async function removeDevice(mac: string) {
    setBusy(true);
    try {
      const r = await fetch(`/api/network/devices/${mac}`, { method: 'DELETE' });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        alert(`Cannot remove: ${j.error || r.status}`);
      }
      setConfirmRemoveMac(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  if (!isActive) return null;

  const all = data?.devices || [];
  const filtered = all.filter((d) => {
    const isWireless = !!d.last_ap;
    if (connFilter === 'wireless' && !isWireless) return false;
    if (connFilter === 'wired' && isWireless) return false;
    if (unnamedOnly && d.friendly_name) return false;
    if (apFilter && d.last_ap !== apFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      const hay = `${d.friendly_name} ${d.mac} ${d.last_ip || ''} ${d.vendor || ''} ${d.last_hostname || ''} ${d.nbns_name || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const onlineCount = all.filter((d) => d.online).length;

  const wirelessCount = all.filter((d) => d.last_ap).length;
  const wiredCount = all.length - wirelessCount;

  return (
    <div id="page-network" className="page active">
      <div className="costs-toolbar">
        <div className="page-title">Network Devices</div>
        <span className="costs-rates-note">
          {data && data.last_poll_ts ? `Scanned ${relativeTime(data.last_poll_ts)}` : ''}
          {data && !data.enabled ? ' · polling disabled' : ''}
        </span>
      </div>

      {/* Summary cards (mirrors Energy Breakdown) */}
      <div className="costs-summary">
        <div className="costs-summary-col">
          <div className="costs-summary-label">Total</div>
          <div className="costs-summary-value">{all.length}</div>
        </div>
        <div className="costs-summary-col">
          <div className="costs-summary-label" style={{ color: 'var(--green)' }}>Online</div>
          <div className="costs-summary-value" style={{ color: 'var(--green)' }}>{onlineCount}</div>
        </div>
        <div
          className="costs-summary-col"
          role="button"
          tabIndex={0}
          onClick={() => setConnFilter(connFilter === 'wireless' ? '' : 'wireless')}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setConnFilter(connFilter === 'wireless' ? '' : 'wireless'); } }}
          style={{
            cursor: 'pointer',
            background: connFilter === 'wireless' ? 'rgba(74,222,128,0.08)' : undefined,
            boxShadow: connFilter === 'wireless' ? 'inset 0 -2px 0 var(--green)' : undefined,
          }}
          title={connFilter === 'wireless' ? 'Click to clear filter' : 'Click to show wireless only'}
        >
          <div className="costs-summary-label">Wireless</div>
          <div className="costs-summary-value">{wirelessCount}</div>
        </div>
        <div
          className="costs-summary-col"
          role="button"
          tabIndex={0}
          onClick={() => setConnFilter(connFilter === 'wired' ? '' : 'wired')}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setConnFilter(connFilter === 'wired' ? '' : 'wired'); } }}
          style={{
            cursor: 'pointer',
            background: connFilter === 'wired' ? 'rgba(74,222,128,0.08)' : undefined,
            boxShadow: connFilter === 'wired' ? 'inset 0 -2px 0 var(--green)' : undefined,
          }}
          title={connFilter === 'wired' ? 'Click to clear filter' : 'Click to show wired only'}
        >
          <div className="costs-summary-label">Wired</div>
          <div className="costs-summary-value">{wiredCount}</div>
        </div>
      </div>

      {/* Filter row */}
      <div className="costs-filters">
        <input
          type="search"
          placeholder="Search name, MAC, IP, vendor"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="costs-date-input"
          style={{ flex: '1 1 240px', minWidth: 200 }}
        />
        <label className="costs-filter-label">
          <input type="checkbox" checked={unnamedOnly} onChange={(e) => setUnnamedOnly(e.target.checked)} />
          Unnamed only
        </label>
        <select value={apFilter} onChange={(e) => setApFilter(e.target.value)}
                className="costs-date-input">
          <option value="">All APs</option>
          {(data?.aps || []).map((ap) => (
            <option key={ap} value={ap}>{ap}</option>
          ))}
        </select>
        <span className="costs-total-badge">
          {filtered.length === all.length
            ? `${all.length} devices`
            : `${filtered.length} of ${all.length}`}
        </span>
      </div>

      {data && data.quarantined_aps.length > 0 && (
        <div style={{ fontSize: 12, color: 'var(--amber)' }}>
          Quarantined APs (won't be polled until httpd recovers): {data.quarantined_aps.map((q) => q.name).join(', ')}
        </div>
      )}

      <div className="net-table-wrap">
        <table className="net-table">
          <thead>
            <tr>
              <th style={{ width: 24 }}></th>
              <th>Friendly Name</th>
              <th>IP</th>
              <th>MAC</th>
              <th>Vendor</th>
              <th>Conn</th>
              <th>AP / Radio / Signal</th>
              <th style={{ textAlign: 'right' }}>First Seen</th>
              <th style={{ textAlign: 'right' }}>Last Seen</th>
              <th style={{ width: 36 }}></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((d) => {
              const wireless = !!d.last_ap;
              const dnsName = d.last_hostname || d.nbns_name || '';
              const tier = stalenessTier(d.last_seen, d.online);
              const isConfirming = confirmRemoveMac === d.mac;
              return (
                <tr key={d.mac} data-stale={tier === 'live' ? undefined : tier}>
                  <td>
                    <span title={d.online ? 'Online' : 'Offline'} style={{
                      display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
                      background: d.online ? 'var(--green, #4ade80)' : 'var(--muted, #555)',
                    }} />
                  </td>
                  <td style={{ minWidth: 180 }}>
                    {editingMac === d.mac ? (
                      <input
                        ref={editInputRef}
                        type="text"
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        onBlur={saveEdit}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') saveEdit();
                          if (e.key === 'Escape') cancelEdit();
                        }}
                        style={{ width: '100%', padding: '3px 6px',
                                 background: 'var(--bg-2, #1a1a1a)',
                                 border: '1px solid var(--accent, #4ade80)',
                                 color: 'inherit', borderRadius: 3 }}
                      />
                    ) : (
                      <span
                        onClick={() => startEdit(d)}
                        style={{
                          cursor: 'pointer',
                          color: d.friendly_name ? 'inherit' : 'var(--muted, #888)',
                          fontStyle: d.friendly_name ? 'normal' : 'italic',
                        }}
                        title={dnsName ? `DNS: ${dnsName}` : 'Click to label'}
                      >
                        {d.friendly_name || dnsName || '—'}
                      </span>
                    )}
                  </td>
                  <td style={{ fontFamily: 'monospace' }}>{d.last_ip || ''}</td>
                  <td style={{ fontFamily: 'monospace', fontSize: 12 }}>{d.mac}</td>
                  <td style={{ color: 'var(--muted, #888)' }}>
                    {d.vendor || ''}
                  </td>
                  <td>
                    <span title={wireless ? 'Wireless' : 'Wired'} style={{ fontSize: 14 }}>
                      {wireless ? '📶' : '🔌'}
                    </span>
                  </td>
                  <td style={{ color: 'var(--muted, #888)' }}>
                    {wireless ? (
                      <>
                        {d.last_ap}
                        {d.last_iface ? ` · ${radioLabel(d.last_iface)}` : ''}
                        {d.last_signal != null ? ` · ${d.last_signal}dBm` : ''}
                      </>
                    ) : ''}
                  </td>
                  <td style={{ textAlign: 'right',
                               color: 'var(--muted, #888)', whiteSpace: 'nowrap' }}
                      title={d.first_seen ? new Date(d.first_seen * 1000).toLocaleString() : ''}>
                    {shortDate(d.first_seen)}
                  </td>
                  <td style={{ textAlign: 'right',
                               color: 'var(--muted, #888)', whiteSpace: 'nowrap' }}>
                    {relativeTime(d.last_seen)}
                  </td>
                  <td style={{ textAlign: 'center', whiteSpace: 'nowrap' }}>
                    {wireless && (
                      <button
                        onClick={() => setPinDevice(d)}
                        title="Pin to AP / band"
                        style={{
                          background: 'none', border: 'none', cursor: 'pointer',
                          fontSize: 14, padding: '2px 6px', color: 'var(--dim)',
                        }}
                      >
                        📌
                      </button>
                    )}
                    {tier === 'cold' && !isConfirming && (
                      <button
                        className="net-remove-btn"
                        onClick={() => setConfirmRemoveMac(d.mac)}
                        title={`Last seen ${shortDate(d.last_seen)} (>90d ago). Remove this entry from the dashboard. (DD-WRT MAC filter bans for this device, if any, are NOT touched.)`}
                      >
                        ✕
                      </button>
                    )}
                    {tier === 'cold' && isConfirming && (
                      <span className="net-remove-confirm" title="Last seen too long ago — remove?">
                        <button className="yes" disabled={busy}
                                onClick={() => removeDevice(d.mac)}>
                          Remove
                        </button>
                        <button className="no" disabled={busy}
                                onClick={() => setConfirmRemoveMac(null)}>
                          Cancel
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={10} style={{ padding: '24px 8px', textAlign: 'center',
                                          color: 'var(--muted, #888)' }}>
                  {data ? 'No devices match the current filters.' :
                          'Loading… (or polling not enabled in Settings)'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <NetworkPinModal
        open={pinDevice !== null}
        mac={pinDevice?.mac || ''}
        friendlyName={pinDevice?.friendly_name || pinDevice?.last_hostname || ''}
        currentAp={pinDevice?.last_ap || null}
        currentRadio={pinDevice?.last_iface || null}
        currentSignal={pinDevice?.last_signal ?? null}
        onClose={() => setPinDevice(null)}
        onApplied={refresh}
      />
    </div>
  );
}
