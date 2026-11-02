'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { ErrorDetail } from '@/lib/api/services/admin';

function formatTimestamp(error: ErrorDetail): string {
  if (error.timestamp_utc) {
    const date = new Date(error.timestamp_utc);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleString(undefined, {
        timeZone: 'UTC',
        timeZoneName: 'short',
      });
    }
  }
  // Older servers send an offset-free server-local stamp, not a browser-local date.
  return `${error.timestamp} (server local)`;
}

interface ErrorDetailsTableProps {
  errors: ErrorDetail[];
}

export function ErrorDetailsTable({ errors }: ErrorDetailsTableProps) {
  if (errors.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Recent Errors</CardTitle>
        </CardHeader>
        <CardContent>
          <p className='text-muted-foreground py-8 text-center'>
            No errors found in the selected time period
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent Errors ({errors.length})</CardTitle>
        <p className='text-muted-foreground text-xs'>
          Timestamps are UTC when available. Older records are shown unchanged
          and marked server local.
        </p>
      </CardHeader>
      <CardContent>
        <div className='max-h-[420px] max-w-full overflow-y-auto'>
          <Table className='min-w-[640px] sm:min-w-[760px]'>
            <TableHeader>
              <TableRow>
                <TableHead>Timestamp (UTC)</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Message</TableHead>
                <TableHead>Location</TableHead>
                <TableHead>Request ID</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {errors.map((error, index) => (
                <TableRow key={index}>
                  <TableCell className='font-mono text-xs'>
                    {formatTimestamp(error)}
                  </TableCell>
                  <TableCell>
                    <Badge variant='destructive'>{error.error_type}</Badge>
                  </TableCell>
                  <TableCell className='max-w-md truncate'>
                    {error.message}
                  </TableCell>
                  <TableCell className='font-mono text-xs'>
                    {error.pathname}:{error.lineno}
                  </TableCell>
                  <TableCell className='font-mono text-xs'>
                    {error.request_id || '-'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
