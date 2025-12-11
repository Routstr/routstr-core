'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Area,
  AreaChart,
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
          <AreaChart data={formattedData}>
            <defs>
              {dataKeys.map((dataKey) => (
                <linearGradient
                  key={dataKey.key}
                  id={`color${dataKey.key}`}
                  x1='0'
                  y1='0'
                  x2='0'
                  y2='1'
                >
                  <stop
                    offset='5%'
                    stopColor={dataKey.color}
                    stopOpacity={0.3}
                  />
                  <stop
                    offset='95%'
                    stopColor={dataKey.color}
                    stopOpacity={0}
                  />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid strokeDasharray='3 3' className='stroke-muted/30' />
            <XAxis
              dataKey='time'
              className='text-xs'
              tick={{ fill: 'hsl(var(--muted-foreground))' }}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
            />
            <YAxis
              className='text-xs'
              tick={{ fill: 'hsl(var(--muted-foreground))' }}
              tickLine={false}
              axisLine={false}
              width={40}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: 'hsl(var(--background))',
                border: '1px solid hsl(var(--border))',
                borderRadius: '8px',
                boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)',
              }}
              itemStyle={{ fontSize: '12px' }}
              labelStyle={{
                fontSize: '12px',
                color: 'hsl(var(--muted-foreground))',
                marginBottom: '8px',
              }}
            />
            <Legend wrapperStyle={{ fontSize: '12px', paddingTop: '16px' }} />
            {dataKeys.map((dataKey) => (
              <Area
                key={dataKey.key}
                type='monotone'
                dataKey={dataKey.key}
                stroke={dataKey.color}
                fillOpacity={1}
                fill={`url(#color${dataKey.key})`}
                name={dataKey.name}
                strokeWidth={2}
                animationDuration={1000}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
