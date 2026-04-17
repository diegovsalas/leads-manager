const styles: Record<string, string> = {
  green: 'bg-green-100 text-green-700',
  yellow: 'bg-yellow-100 text-yellow-700',
  red: 'bg-red-100 text-red-700',
};

export default function HealthBadge({ score, categoria, color }: { score: number; categoria: string; color: string }) {
  return (
    <span className={`inline-block px-2.5 py-0.5 rounded-full text-xs font-semibold ${styles[color] || styles.red}`}>
      {categoria} · {score}
    </span>
  );
}
