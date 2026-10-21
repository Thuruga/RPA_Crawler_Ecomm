import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import { EmptyState } from '../../components/ui/Feedback';

export type PriceHistoryPoint = {
  timestamp: string | number | Date;
  price: number;
  [key: string]: unknown;
};

export type PriceChartProps = {
  history: PriceHistoryPoint[];
  emptyTitle?: string;
  emptyDescription?: string;
};

export const PriceChart = ({
  history,
  emptyTitle = 'Histórico ainda não disponível',
  emptyDescription = 'Aguardando a primeira variação de preço para gerar o gráfico.',
}: PriceChartProps) => {
  if (!history || history.length === 0) {
    return (
      <div className="monitor-chart-container">
        <EmptyState title={emptyTitle} description={emptyDescription} />
      </div>
    );
  }

  const deduplicatedHistory = history.filter((h, index, arr) => {
    if (index === 0) return true;
    return h.price !== arr[index - 1].price;
  });

  const data = deduplicatedHistory.map(h => {
    const d = new Date(h.timestamp);
    const dateStr = d.toLocaleDateString([], { day: '2-digit', month: '2-digit' });
    const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return {
      time: `${dateStr} ${timeStr}`,
      fullDate: d.toLocaleString(),
      price: h.price
    };
  });

  // Se tiver apenas 1 ponto, duplicamos para mostrar uma linha estável até o momento atual
  const chartData = data.length === 1
    ? [data[0], { ...data[0], time: 'Agora', fullDate: 'Agora (Sem novas variações)' }]
    : data;

  return (
    <div className="monitor-chart-container monitor-chart">
      <div className="monitor-chart__header">
        <span>Histórico de Variações</span>
        <span>{data.length} registros</span>
      </div>
      <div className="monitor-chart__canvas">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" vertical={false} />
            <XAxis
              dataKey="time"
              stroke="var(--color-text-muted)"
              fontSize={12}
              tickLine={false}
              axisLine={false}
              dy={10}
            />
            <YAxis
              stroke="var(--color-text-muted)"
              fontSize={12}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => `R$${value}`}
              domain={['auto', 'auto']}
              dx={-10}
            />
            <Tooltip
              contentStyle={{ backgroundColor: 'var(--color-surface)', border: 'var(--border-default)', borderRadius: 'var(--radius-sm)', fontSize: '12px', color: 'var(--color-text)' }}
              itemStyle={{ color: 'var(--color-action-primary)' }}
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              formatter={(value: any) => [`R$ ${value.toFixed(2)}`, 'Preço']}
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              labelFormatter={(label, items) => (items[0] as any)?.payload?.fullDate || label}
            />
            <Line
              type="monotone"
              dataKey="price"
              stroke="var(--color-action-primary)"
              strokeWidth={3}
              dot={{ r: 4, fill: 'var(--color-action-primary)', strokeWidth: 2, stroke: 'var(--color-surface)' }}
              activeDot={{ r: 6, strokeWidth: 0 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
};
