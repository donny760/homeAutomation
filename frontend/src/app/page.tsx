'use client';

import { useState, useEffect } from 'react';
import Nav, { PageName } from '@/components/Nav';
import Dashboard from '@/components/Dashboard';
import EventLog from '@/components/EventLog';
import Rules from '@/components/Rules';
import EnergyCosts from '@/components/EnergyCosts';
import Settings from '@/components/Settings';

export default function Home() {
  const [activePage, setActivePage] = useState<PageName>('dashboard');

  useEffect(() => {
    // Restore page from URL hash on initial load
    const hash = location.hash.replace('#', '') as PageName;
    if (hash && ['dashboard', 'events', 'rules', 'costs', 'settings'].includes(hash)) {
      setActivePage(hash);
    } else {
      history.replaceState({ page: 'dashboard' }, '', '#dashboard');
    }

    const onPopState = (e: PopStateEvent) => {
      const name = (e.state?.page || location.hash.replace('#', '') || 'dashboard') as PageName;
      setActivePage(name);
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  function handlePageChange(page: PageName) {
    setActivePage(page);
    history.pushState({ page }, '', '#' + page);
  }

  return (
    <>
      <Nav activePage={activePage} onPageChange={handlePageChange} />
      {activePage === 'dashboard' && <Dashboard />}
      {activePage === 'events' && <EventLog isActive={activePage === 'events'} />}
      {activePage === 'rules' && <Rules isActive={activePage === 'rules'} />}
      {activePage === 'costs' && <EnergyCosts isActive={activePage === 'costs'} />}
      {activePage === 'settings' && <Settings isActive={activePage === 'settings'} />}
    </>
  );
}
