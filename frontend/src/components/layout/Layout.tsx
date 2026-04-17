import { Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import Sidebar from './Sidebar';
import { csApi } from '../../api/client';

export default function Layout() {
  const { data: user } = useQuery({
    queryKey: ['me'],
    queryFn: csApi.me,
  });

  return (
    <div className="flex h-screen bg-[#f5f5f9]">
      <Sidebar user={user ?? { nombre: '...', rol: '' }} />
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-[1400px] mx-auto px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
