interface Props {
  label: string;
  value: string | number;
  sub?: string;
  color?: string;
  delta?: number | null;
}

export default function KpiCard({ label, value, sub, color = 'gray', delta }: Props) {
  return (
    <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-5 hover:shadow-md transition-shadow">
      <p className="text-xs text-gray-400 uppercase tracking-wider font-semibold mb-2">{label}</p>
      <p className={`text-2xl font-bold text-${color}-800`}>{value}</p>
      <div className="flex items-center gap-2 mt-1">
        {sub && <p className="text-xs text-gray-400">{sub}</p>}
        {delta != null && (
          <span className={`text-xs font-semibold ${delta >= 0 ? 'text-green-600' : 'text-red-500'}`}>
            {delta >= 0 ? '+' : ''}{delta}% vs anterior
          </span>
        )}
      </div>
    </div>
  );
}
