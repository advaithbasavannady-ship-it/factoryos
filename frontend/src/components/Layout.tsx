import { Outlet } from 'react-router-dom';

import Header from '@/components/Header';
import Sidebar from '@/components/Sidebar';

export default function Layout() {
  return (
    <div className="app-shell">
      <Sidebar />
      <div className="main-column">
        <Header />
        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
