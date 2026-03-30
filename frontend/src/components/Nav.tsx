'use client';

import { useEffect, useState } from 'react';

export type PageName = 'dashboard' | 'events' | 'rules' | 'costs' | 'settings';

interface NavProps {
  activePage: PageName;
  onPageChange: (page: PageName) => void;
}

const PAGES: { key: PageName; label: string; sparkle?: boolean }[] = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'events', label: 'Event Log' },
  { key: 'rules', label: 'Powerwall Rules', sparkle: true },
  { key: 'costs', label: 'Energy Breakdown' },
  { key: 'settings', label: 'Settings' },
];

export default function Nav({ activePage, onPageChange }: NavProps) {
  const [isLight, setIsLight] = useState(false);

  useEffect(() => {
    if (localStorage.getItem('theme') === 'light') {
      document.body.classList.add('light');
      setIsLight(true);
    }
  }, []);

  function toggleTheme() {
    const light = document.body.classList.toggle('light');
    localStorage.setItem('theme', light ? 'light' : 'dark');
    setIsLight(light);
    // dispatch custom event so DayChart can sync theme
    window.dispatchEvent(new CustomEvent('themechange', { detail: { light } }));
  }

  return (
    <nav className="nav">
      <div className="nav-links">
        {PAGES.map((p) => (
          <button
            key={p.key}
            className={`nav-link${activePage === p.key ? ' active' : ''}`}
            data-page={p.key}
            onClick={() => onPageChange(p.key)}
          >
            {p.sparkle && <span className="nav-sparkle">&#10022;</span>}{' '}
            {p.label}
          </button>
        ))}
      </div>
      <button className="theme-btn" onClick={toggleTheme}>
        {isLight ? 'Dark' : 'Light'}
      </button>
    </nav>
  );
}
