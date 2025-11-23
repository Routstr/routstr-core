'use client';

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { ModelRevenueData } from '@/lib/api/services/admin';
import { useCurrencyStore } from '@/lib/stores/currency';
import { useQuery } from '@tanstack/react-query';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { formatFromMsat, convertToMsat } from '@/lib/currency';

interface RevenueByModelTableProps {
  models: ModelRevenueData[];
  totalRevenue: number;
}

export function RevenueByModelTable({
  models,
  totalRevenue,
}: RevenueByModelTableProps) {
  const { displayUnit } = useCurrencyStore();
  const { data: btcUsdPrice } = useQuery({
    queryKey: ['btc-usd-price'],
    queryFn: fetchBtcUsdPrice,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });
  const usdPerSat = btcUsdPrice ? btcToSatsRate(btcUsdPrice) : null;

  const formatAmount = (sats: number) =>
    formatFromMsat(convertToMsat(sats, 'sat'), displayUnit, usdPerSat);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Revenue by Model</CardTitle>
        <p className="text-sm text-muted-foreground">
          Total Revenue: <span className="font-mono font-medium text-foreground">{formatAmount(totalRevenue)}</span>
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead className="text-right">Requests</TableHead>
              <TableHead className="text-right">Successful</TableHead>
              <TableHead className="text-right">Failed</TableHead>
              <TableHead className="text-right">Revenue</TableHead>
              <TableHead className="w-[100px]">Share</TableHead>
              <TableHead className="text-right">Refunds</TableHead>
              <TableHead className="text-right">Net Revenue</TableHead>
              <TableHead className="text-right">Avg/Request</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {models.length === 0 ? (
              <TableRow>
                <TableCell colSpan={9} className="text-center text-muted-foreground">
                  No model data available
                </TableCell>
              </TableRow>
            ) : (
              models.map((model) => {
                const share = totalRevenue > 0 ? (model.revenue_sats / totalRevenue) * 100 : 0;
                return (
                  <TableRow key={model.model}>
                    <TableCell className="font-medium">{model.model}</TableCell>
                    <TableCell className="text-right font-mono">{model.requests}</TableCell>
                    <TableCell className="text-right text-green-600 font-mono">{model.successful}</TableCell>
                    <TableCell className="text-right text-red-600 font-mono">{model.failed}</TableCell>
                    <TableCell className="text-right font-mono">
                      {formatAmount(model.revenue_sats)}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Progress value={share} className="h-2" />
                        <span className="text-xs text-muted-foreground w-8 text-right">{share.toFixed(0)}%</span>
                      </div>
                    </TableCell>
                    <TableCell className="text-right text-red-500 font-mono">
                      {formatAmount(model.refunds_sats)}
                    </TableCell>
                    <TableCell className="text-right font-semibold font-mono">
                      {formatAmount(model.net_revenue_sats)}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground font-mono">
                      {formatAmount(model.avg_revenue_per_request)}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
