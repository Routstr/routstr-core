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
          <p className='text-muted-foreground text-center py-8'>
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
      </CardHeader>
      <CardContent>
        <div className='max-h-[400px] overflow-y-auto'>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Timestamp</TableHead>
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
                    {new Date(error.timestamp).toLocaleString()}
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
