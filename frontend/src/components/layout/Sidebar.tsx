import { NavLink } from 'react-router-dom';

const links = [
  { to: '/app', icon: '📊', label: 'Dashboard', end: true },
  { to: '/app/clientes', icon: '🏢', label: 'Clientes' },
  { to: '/app/contactos', icon: '👥', label: 'Contactos' },
  { to: '/app/kams', icon: '👤', label: 'KAMs' },
  { to: '/app/alertas', icon: '🔔', label: 'Alertas' },
];

export default function Sidebar({ user }: { user: { nombre: string; rol: string } }) {
  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-2 px-3.5 py-2 rounded-md text-[13px] mx-1 transition-all ${
      isActive
        ? 'bg-white/12 text-purple-300 font-medium'
        : 'text-white/55 hover:bg-white/8 hover:text-white/90'
    }`;

  return (
    <aside className="w-[200px] bg-[#1e1b3a] flex flex-col h-screen sticky top-0">
      <div className="px-3.5 pt-3 pb-5">
        <span className="text-base font-extrabold text-white tracking-tight">
          <span className="text-purple-400">CRM</span>AVANTEX
        </span>
      </div>

      <nav className="flex-1 flex flex-col gap-0.5">
        {links.map((l) => (
          <NavLink key={l.to} to={l.to} end={l.end} className={linkClass}>
            <span>{l.icon}</span> {l.label}
          </NavLink>
        ))}

        <div className="h-px bg-white/8 mx-3.5 my-2" />

        <a href="/cs/" className="flex items-center gap-2 px-3.5 py-2 mx-1 text-[13px] text-white/35 hover:text-white/60 transition-colors">
          📋 CS Legacy
        </a>
        <a href="/" className="flex items-center gap-2 px-3.5 py-2 mx-1 text-[13px] text-white/35 hover:text-white/60 transition-colors">
          ← CRM Pipeline
        </a>
      </nav>

      <div className="p-3 border-t border-white/8">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-full bg-purple-600 text-white flex items-center justify-center text-[10px] font-bold">
            {user.nombre.slice(0, 2).toUpperCase()}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-medium text-white/85 truncate">{user.nombre}</div>
            <div className="text-[11px] text-white/40">{user.rol}</div>
          </div>
          <a href="/logout" className="text-[11px] text-white/35 hover:text-white/60">⏻</a>
        </div>
      </div>
    </aside>
  );
}
