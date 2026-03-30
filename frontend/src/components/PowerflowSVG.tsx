'use client';

import { useRef, useCallback, useEffect } from 'react';
import { fmtW, fmtWabs, modeLabel } from '@/lib/format';

const THRESHOLD = 50;

export interface LiveData {
  solar_w: number;
  battery_w: number;
  grid_w: number;
  home_w: number;
  battery_pct?: number;
  battery_status?: string;
  solar_kwh_today?: number;
  grid_kwh_today?: number;
  mode?: string;
}

interface PowerflowSVGProps {
  data: LiveData | null;
}

export default function PowerflowSVG({ data }: PowerflowSVGProps) {
  const svgRef = useRef<SVGSVGElement>(null);

  const setFlow = useCallback((id: string, active: boolean, watts: number) => {
    const el = svgRef.current?.getElementById(id) as SVGElement | null;
    if (!el) return;
    if (active && watts > THRESHOLD) {
      el.classList.add('active');
      const dur = Math.max(0.4, Math.min(2.5, 400 / Math.sqrt(watts + 100)));
      el.style.setProperty('--dur', dur + 's');
    } else {
      el.classList.remove('active');
    }
  }, []);

  useEffect(() => {
    if (!data) return;
    const d = data;

    const solarOn = d.solar_w > THRESHOLD;
    const battChg = d.battery_w > THRESHOLD;
    const battDis = d.battery_w < -THRESHOLD;
    const gridIn = d.grid_w > THRESHOLD;
    const gridOut = d.grid_w < -THRESHOLD;

    setFlow('flow-solar-home', solarOn, d.solar_w);
    setFlow('flow-solar-battery', battChg, d.battery_w);
    setFlow('flow-solar-grid', gridOut, Math.abs(d.grid_w));
    setFlow('flow-battery-home', battDis, Math.abs(d.battery_w));
    setFlow('flow-grid-home', gridIn, d.grid_w);
    setFlow('flow-battery-grid', battDis && gridOut, Math.abs(d.battery_w));

    const svg = svgRef.current;
    if (!svg) return;

    const setText = (id: string, text: string) => {
      const el = svg.getElementById(id);
      if (el) el.textContent = text;
    };
    const setAttr = (id: string, attr: string, val: string) => {
      const el = svg.getElementById(id);
      if (el) el.setAttribute(attr, val);
    };
    const setStyle = (id: string, prop: string, val: string) => {
      const el = svg.getElementById(id) as HTMLElement | null;
      if (el) el.style.setProperty(prop, val);
    };

    // Solar pill
    setText('p-solar-w', fmtW(d.solar_w));
    setText('p-solar-today', (d.solar_kwh_today || 0).toFixed(1) + ' kWh today');

    // Battery pill
    setText('p-batt-w', fmtWabs(d.battery_w));
    setText('p-batt-pct', Math.round(d.battery_pct || 0) + '%');
    setText('p-batt-status', d.battery_status ? '\u00b7 ' + d.battery_status : '');

    // Home pill
    setText('p-home-w', fmtW(d.home_w));

    // Grid pill
    setText('p-grid-w', fmtWabs(d.grid_w));
    setText('p-grid-dir', gridIn ? 'importing' : gridOut ? 'exporting' : '');
    const gridDot = svg.getElementById('p-grid-dot') as HTMLElement | null;
    if (gridDot) {
      gridDot.style.background = gridOut ? '#1D9E75' : '#888780';
    }
    const pGridW = svg.getElementById('p-grid-w') as HTMLElement | null;
    if (pGridW) {
      pGridW.style.color = gridOut ? '#1D9E75' : '#888780';
    }

    // Battery icon fill width
    const fillEl = svg.getElementById('batt-icon-fill');
    if (fillEl) fillEl.setAttribute('width', String(Math.max(2, Math.round(22 * (d.battery_pct || 0) / 100))));

    // Battery ring color
    const ringColor = (d.battery_pct || 0) > 30 ? '#1D9E75' : (d.battery_pct || 0) > 15 ? '#EF9F27' : '#e05252';
    setAttr('batt-flow-ring', 'stroke', ringColor);

    // Grid today
    if (d.grid_kwh_today != null) {
      setText('p-grid-today', d.grid_kwh_today.toFixed(1) + ' kWh today');
    }

    // Mode label
    if (d.mode) {
      const label = modeLabel(d.mode) || d.mode;
      const modeColors: Record<string, string> = {
        self_consumption: '#1D9E75',
        autonomous: '#EF9F27',
        backup: '#e05252',
      };
      const col = modeColors[d.mode] || '#888780';
      const pMode = svg.getElementById('p-mode');
      if (pMode) {
        pMode.textContent = label.toUpperCase();
        pMode.setAttribute('fill', col);
      }
      setAttr('p-mode-accent', 'fill', col);
      setAttr('p-mode-dot', 'fill', col);
      const pModeBg = svg.getElementById('p-mode-bg') as SVGElement | null;
      if (pModeBg) {
        pModeBg.setAttribute('stroke', col);
        (pModeBg as any).style.opacity = '0.85';
        const w = Math.max(100, label.length * 6.8 + 42);
        pModeBg.setAttribute('width', String(w));
      }
    }
  }, [data, setFlow]);

  return (
    <div className="card flow-card">
      <svg ref={svgRef} id="flow-svg" viewBox="0 0 900 360" preserveAspectRatio="xMidYMid meet">
        {/* Base paths */}
        <path className="base-path" d="M 450,55 C 640,55 690,130 690,185" />
        <path className="base-path" d="M 450,55 C 260,55 210,130 210,185" />
        <path className="base-path" d="M 450,55 L 450,295" />
        <path className="base-path" d="M 450,295 C 640,295 690,250 690,185" />
        <path className="base-path" d="M 210,185 L 690,185" />
        <path className="base-path" d="M 210,185 C 210,280 350,295 450,295" />

        {/* Animated flow overlays */}
        <path id="flow-solar-home" className="flow-path" stroke="#EF9F27" d="M 450,55 C 640,55 690,130 690,185" />
        <path id="flow-solar-battery" className="flow-path" stroke="#EF9F27" d="M 450,55 C 260,55 210,130 210,185" />
        <path id="flow-solar-grid" className="flow-path" stroke="#EF9F27" d="M 450,55 L 450,295" />
        <path id="flow-battery-home" className="flow-path" stroke="#1D9E75" d="M 210,185 L 690,185" />
        <path id="flow-grid-home" className="flow-path" stroke="#888780" d="M 450,295 C 640,295 690,250 690,185" />
        <path id="flow-battery-grid" className="flow-path" stroke="#1D9E75" d="M 210,185 C 210,280 350,295 450,295" />

        {/* Solar node */}
        <circle cx="450" cy="55" r="36" className="node-ring" stroke="#EF9F27" />
        <circle cx="450" cy="55" r="8" fill="#EF9F27" opacity="0.85" />
        <line x1="450" y1="42" x2="450" y2="38" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />
        <line x1="461" y1="45" x2="464" y2="42" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />
        <line x1="465" y1="55" x2="469" y2="55" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />
        <line x1="461" y1="65" x2="464" y2="68" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />
        <line x1="439" y1="45" x2="436" y2="42" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />
        <line x1="435" y1="55" x2="431" y2="55" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />
        <line x1="439" y1="65" x2="436" y2="68" stroke="#EF9F27" strokeWidth="2.3" strokeLinecap="round" />

        {/* Solar pill leader */}
        <line x1="476" y1="30" x2="496" y2="18" stroke="#333336" strokeWidth="0.5" strokeDasharray="3 3" />
        <circle cx="476" cy="30" r="3" fill="#EF9F27" />
        <foreignObject x="496" y="5" width="170" height="28">
          <div className="pill">
            <span className="pill-dot" style={{ background: '#EF9F27' }} />
            <span className="pill-lbl">Solar</span>
            <span id="p-solar-w" className="pill-val" style={{ color: '#EF9F27' }}>0 W</span>
          </div>
        </foreignObject>

        {/* Solar today text */}
        <rect x="466" y="91" width="110" height="16" rx="4" fill="var(--card-bg)" opacity="0.85" />
        <text id="p-solar-today" x="470" y="103" textAnchor="start" fontSize="12" fill="#EF9F27">-- kWh today</text>

        {/* Battery node */}
        <circle id="batt-flow-ring" cx="210" cy="185" r="36" className="node-ring" stroke="#1D9E75" />
        <rect x="197" y="178" width="26" height="14" rx="3" fill="none" stroke="#1D9E75" strokeWidth="1.8" />
        <rect x="223" y="182" width="4" height="6" rx="1" fill="#1D9E75" />
        <rect id="batt-icon-fill" x="199" y="180" width="13" height="10" rx="2" fill="#1D9E75" opacity="0.75" />

        {/* Battery pill leader */}
        <line x1="186" y1="158" x2="130" y2="95" stroke="#333336" strokeWidth="0.5" strokeDasharray="3 3" />
        <circle cx="186" cy="158" r="3" fill="#1D9E75" />
        <foreignObject x="5" y="78" width="230" height="28">
          <div className="pill">
            <span className="pill-dot" style={{ background: '#1D9E75' }} />
            <span className="pill-lbl">Battery</span>
            <span id="p-batt-w" className="pill-val" style={{ color: '#1D9E75' }}>0 W</span>
            <span className="pill-lbl">&middot;</span>
            <span id="p-batt-pct" className="pill-val" style={{ color: '#1D9E75' }}>--%</span>
            <span id="p-batt-status" className="pill-lbl" />
          </div>
        </foreignObject>

        {/* Home node */}
        <circle cx="690" cy="185" r="36" className="node-ring" stroke="#378ADD" />
        <polygon points="691,167 674,182 674,202 708,202 708,182" fill="none" stroke="#378ADD" strokeWidth="1.8" strokeLinejoin="round" />
        <polygon points="691,162 670,180 712,180" fill="#378ADD" opacity="0.7" />
        <rect x="685" y="192" width="12" height="10" rx="1" className="node-interior" />

        {/* Home pill leader */}
        <line x1="718" y1="163" x2="720" y2="148" stroke="#333336" strokeWidth="0.5" strokeDasharray="3 3" />
        <circle cx="718" cy="163" r="3" fill="#378ADD" />
        <foreignObject x="720" y="128" width="150" height="28">
          <div className="pill">
            <span className="pill-dot" style={{ background: '#378ADD' }} />
            <span className="pill-lbl">Home</span>
            <span id="p-home-w" className="pill-val" style={{ color: '#378ADD' }}>0 W</span>
          </div>
        </foreignObject>

        {/* Mode label */}
        <rect id="p-mode-bg" x="8" y="8" width="150" height="26" rx="6" fill="#18181a" stroke="#333336" strokeWidth="1" />
        <rect id="p-mode-accent" x="8" y="8" width="4" height="26" rx="3" fill="#888780" />
        <circle id="p-mode-dot" cx="22" cy="21" r="4" fill="#888780" opacity="0.9" />
        <text x="30" y="17" fontSize="8" fill="#5a5a5e" letterSpacing="0.8">MODE</text>
        <text id="p-mode" x="30" y="29" fontSize="11" fill="#888780" fontWeight="600" letterSpacing="0.2">&mdash;</text>

        {/* Grid today text */}
        <rect x="466" y="253" width="110" height="16" rx="4" fill="var(--card-bg)" opacity="0.85" />
        <text id="p-grid-today" x="472" y="263" textAnchor="start" fontSize="12" fill="#888780">-- kWh today</text>

        {/* Grid node */}
        <circle id="grid-ring" cx="450" cy="295" r="36" className="node-ring" stroke="#888780" />
        <polygon points="454,275 446,295 452,295 447,315 458,295 451,295" fill="#888780" opacity="0.8" />

        {/* Grid pill leader */}
        <line x1="480" y1="315" x2="498" y2="325" stroke="#333336" strokeWidth="0.5" strokeDasharray="3 3" />
        <circle cx="480" cy="315" r="3" fill="#888780" />
        <foreignObject x="498" y="312" width="200" height="28">
          <div className="pill">
            <span className="pill-dot" id="p-grid-dot" style={{ background: '#888780' }} />
            <span className="pill-lbl">Grid</span>
            <span id="p-grid-w" className="pill-val" style={{ color: '#888780' }}>0 W</span>
            <span id="p-grid-dir" className="pill-lbl" />
          </div>
        </foreignObject>
      </svg>
    </div>
  );
}
