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
import { ModelRevenueData } from '@/lib/api/services/admin';

interface RevenueByModelTableProps {
  models: ModelRevenueData[];
  totalRevenue: number;
}

export function RevenueByModelTable({
  models,
  totalRevenue,
}: RevenueByModelTableProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Revenue by Model</CardTitle>
        <p className='text-sm text-muted-foreground'>
          Total Revenue:{' '}
          {totalRevenue.toLocaleString(undefined, {
            maximumFractionDigits: 2,
          })}{' '}
          sats
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead className='text-right'>Requests</TableHead>
              <TableHead className='text-right'>Successful</TableHead>
              <TableHead className='text-right'>Failed</TableHead>
              <TableHead className='text-right'>Revenue (sats)</TableHead>
              <TableHead className='text-right'>Refunds (sats)</TableHead>
              <TableHead className='text-right'>Net Revenue (sats)</TableHead>
              <TableHead className='text-right'>Avg/Request</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {models.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className='text-center text-muted-foreground'>
                  No model data available
                </TableCell>
              </TableRow>
            ) : (
              models.map((model) => (
                <TableRow key={model.model}>
                  <TableCell className='font-medium'>{model.model}</TableCell>
                  <TableCell className='text-right'>{model.requests}</TableCell>
                  <TableCell className='text-right text-green-600'>
                    {model.successful}
                  </TableCell>
                  <TableCell className='text-right text-red-600'>
                    {model.failed}
                  </TableCell>
                  <TableCell className='text-right'>
                    {model.revenue_sats.toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    })}
                  </TableCell>
                  <TableCell className='text-right text-red-500'>
                    {model.refunds_sats.toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    })}
                  </TableCell>
                  <TableCell className='text-right font-semibold'>
                    {model.net_revenue_sats.toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    })}
                  </TableCell>
                  <TableCell className='text-right text-muted-foreground'>
                    {model.avg_revenue_per_request.toLocaleString(undefined, {
                      maximumFractionDigits: 3,
                    })}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
