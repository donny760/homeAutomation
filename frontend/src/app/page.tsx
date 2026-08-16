'use client';

import { useState, useEffect } from 'react';
import Nav, { PageName, isPageName } from '@/components/Nav';
import Dashboard from '@/components/Dashboard';
import EventLog from '@/components/EventLog';
import Rules from '@/components/Rules';
import EnergyCosts from '@/components/EnergyCosts';
import Settings from '@/components/Settings';
import NetworkDevices from '@/components/NetworkDevices';
import SwitchesDrawer from '@/components/SwitchesDrawer';

export default function Home() {
  const [activePage, setActivePage] = useState<PageName>('dashboard');
  const [switchesOpen, setSwitchesOpen] = useState(false);

  useEffect(() => {
    // Restore page from URL hash on initial load
    const hash = location.hash.replace('#', '');
    if (isPageName(hash)) {
      setActivePage(hash);
    } else {
      history.replaceState({ page: 'dashboard' }, '', '#dashboard');
    }

    const onPopState = (e: PopStateEvent) => {
      // Validate here too — a hand-edited hash reaches this path unchecked.
      const name = e.state?.page || location.hash.replace('#', '');
      setActivePage(isPageName(name) ? name : 'dashboard');
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
      <Nav
        activePage={activePage}
        onPageChange={handlePageChange}
        onOpenSwitches={() => setSwitchesOpen(true)}
      />
      {activePage === 'dashboard' && <Dashboard />}
      {activePage === 'events' && <EventLog isActive={activePage === 'events'} />}
      {activePage === 'rules' && <Rules isActive={activePage === 'rules'} />}
      {activePage === 'costs' && <EnergyCosts isActive={activePage === 'costs'} />}
      {activePage === 'network' && <NetworkDevices isActive={activePage === 'network'} />}
      {activePage === 'settings' && <Settings isActive={activePage === 'settings'} />}
      <SwitchesDrawer open={switchesOpen} onClose={() => setSwitchesOpen(false)} />
    </>
  );
}
