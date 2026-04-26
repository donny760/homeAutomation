'use client';

import { useState, useEffect } from 'react';

interface RadioFilter { mode: string | null; enabled: boolean; list: string[]; }
interface ApFilters { ap: string; filters: { wl0?: RadioFilter; wl1?: RadioFilter }; errors: string[]; }
interface ApFiltersResp { aps: ApFilters[]; }

interface PinModalProps {
  open: boolean;
  mac: string;
  friendlyName: string;
  currentAp: string | null;
  currentRadio: string | null;
  currentSignal: number | null;
  onClose: () => void;
  onApplied: () => void;
}

const RADIOS: { key: 'wl0' | 'wl1'; label: string }[] = [
  { key: 'wl0', label: '2.4 GHz' },
  { key: 'wl1', label: '5 GHz' },
];

export default function NetworkPinModal({
  open, mac, friendlyName, currentAp, currentRadio, currentSignal,
  onClose, onApplied,
}: PinModalProps) {
  const [data, setData] = useState<ApFiltersResp | null>(null);
  const [matrix, setMatrix] = useState<Record<'wl0' | 'wl1', Record<string, boolean>>>(
    { wl0: {}, wl1: {} }
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!open) return;
    setErr('');
    setBusy(true);
    fetch('/api/network/ap_filters')
      .then((r) => r.json())
      .then((d: ApFiltersResp) => {
        setData(d);
        // Initialize matrix from current ban state.
        const m: Record<'wl0' | 'wl1', Record<string, boolean>> = { wl0: {}, wl1: {} };
        for (const ap of d.aps || []) {
          for (const radio of ['wl0', 'wl1'] as const) {
            const list = (ap.filters?.[radio]?.list || []).map((s) => s.toLowerCase());
            m[radio][ap.ap] = list.includes(mac.toLowerCase());
          }
        }
        setMatrix(m);
      })
      .catch((e) => setErr(`Load failed: ${e}`))
      .finally(() => setBusy(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mac]);

  if (!open) return null;

  const aps = (data?.aps || []).map((a) => a.ap);

  function toggle(radio: 'wl0' | 'wl1', ap: string) {
    setMatrix((m) => ({
      ...m,
      [radio]: { ...m[radio], [ap]: !m[radio][ap] },
    }));
  }

  function allowEverywhere() {
    const next: Record<'wl0' | 'wl1', Record<string, boolean>> = { wl0: {}, wl1: {} };
    for (const ap of aps) {
      for (const radio of ['wl0', 'wl1'] as const) {
        next[radio][ap] = false;
      }
    }
    setMatrix(next);
  }

  async function applyChanges() {
    setBusy(true);
    setErr('');
    try {
      const r = await fetch(`/api/network/devices/${mac}/filters`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(matrix),
      });
      const j = await r.json();
      if (!j.all_ok) {
        const failed = (j.results || []).filter((x: any) => !x.ok);
        setErr(`${failed.length} write(s) failed: ${failed.map((x: any) => `${x.ap}/${x.radio}`).join(', ')}`);
        setBusy(false);
        return;
      }
      onApplied();
      onClose();
    } catch (e) {
      setErr(`Apply failed: ${e}`);
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ width: 580 }}>
        <div className="modal-title">
          Pin Wireless Device
          <div style={{ fontSize: '0.85rem', color: 'var(--dim)', fontWeight: 400, marginTop: 4 }}>
            {friendlyName || '(unlabeled)'} <span style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>{mac}</span>
          </div>
        </div>

        {currentAp && (
          <div style={{ fontSize: '0.85rem', color: 'var(--dim)', marginBottom: 14 }}>
            Currently associated to <strong style={{ color: 'var(--text)' }}>{currentAp}</strong>
            {currentRadio && ` · ${currentRadio === 'wl0' ? '2.4 GHz' : '5 GHz'}`}
            {currentSignal != null && ` · ${currentSignal} dBm`}
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      marginTop: 6, marginBottom: 6 }}>
          <div className="form-label">Filter state per AP × band</div>
          <button className="btn-cancel" onClick={allowEverywhere} disabled={busy}
                  style={{ fontSize: '0.78rem', padding: '4px 10px' }}>
            Allow everywhere
          </button>
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', padding: '6px 4px', color: 'var(--dim)', fontWeight: 400 }}>AP</th>
              {RADIOS.map((r) => (
                <th key={r.key} style={{ textAlign: 'center', padding: '6px 4px', color: 'var(--dim)', fontWeight: 400 }}>
                  {r.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {aps.map((ap) => (
              <tr key={ap} style={{ borderTop: '0.5px solid var(--border)' }}>
                <td style={{ padding: '8px 4px' }}>{ap}</td>
                {RADIOS.map((r) => {
                  const banned = matrix[r.key]?.[ap];
                  return (
                    <td key={r.key} style={{ textAlign: 'center', padding: '4px' }}>
                      <button
                        onClick={() => toggle(r.key, ap)}
                        disabled={busy}
                        style={{
                          padding: '4px 12px', fontSize: '0.82rem', cursor: 'pointer',
                          border: '0.5px solid var(--border)', borderRadius: 6,
                          background: banned ? '#5a3a3a' : 'var(--bg)',
                          color: banned ? '#ffb0b0' : 'var(--green)',
                          minWidth: 90,
                        }}>
                        {banned ? '● banned' : '○ allowed'}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>

        {err && <div style={{ color: '#e05252', marginTop: 12, fontSize: '0.85rem' }}>{err}</div>}

        <div className="modal-footer">
          <button className="btn-cancel" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-save" onClick={applyChanges} disabled={busy || !data}>
            {busy ? 'Working…' : 'Apply changes'}
          </button>
        </div>
      </div>
    </div>
  );
}
