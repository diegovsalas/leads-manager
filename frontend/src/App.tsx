import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Layout from './components/layout/Layout';
import Dashboard from './pages/Dashboard';
import Clientes from './pages/Clientes';
import Contactos from './pages/Contactos';
import Kams from './pages/Kams';
import Alertas from './pages/Alertas';
import AccountDetail from './pages/AccountDetail';
import Ayuda from './pages/Ayuda';
import Empresas from './pages/Empresas';
import Leads from './pages/Leads';

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/app" element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path="clientes" element={<Clientes />} />
            <Route path="contactos" element={<Contactos />} />
            <Route path="empresas" element={<Empresas />} />
            <Route path="leads" element={<Leads />} />
            <Route path="accounts/:id" element={<AccountDetail />} />
            <Route path="kams" element={<Kams />} />
            <Route path="alertas" element={<Alertas />} />
            <Route path="ayuda" element={<Ayuda />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
