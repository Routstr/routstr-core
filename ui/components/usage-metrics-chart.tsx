'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
  CartesianGrid,
} from 'recharts';

interface UsageMetricsChartProps {
  data: Array<Record<string, unknown> & { timestamp: string }>;
  title: string;
  dataKeys: Array<{
    key: string;
    name: string;
    color: string;
  }>;
}

export function UsageMetricsChart({
  data,
  title,
  dataKeys,
}: UsageMetricsChartProps) {
  const formattedData = data.map((item) => ({
    ...item,
    time: new Date(item.timestamp).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    }),
  }));

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width='100%' height={300}>
          <LineChart data={formattedData}>
            <CartesianGrid strokeDasharray='3 3' className='stroke-muted' />
            <XAxis
              dataKey='time'
              className='text-xs'
              tick={{ fill: 'currentColor' }}
            />
            <YAxis className='text-xs' tick={{ fill: 'currentColor' }} />
            <Tooltip
              contentStyle={{
                backgroundColor: 'hsl(var(--background))',
                border: '1px solid hsl(var(--border))',
                borderRadius: '6px',
              }}
            />
            <Legend />
            {dataKeys.map((dataKey) => (
              <Line
                key={dataKey.key}
                type='monotone'
                dataKey={dataKey.key}
                stroke={dataKey.color}
                name={dataKey.name}
                strokeWidth={2}
                dot={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
