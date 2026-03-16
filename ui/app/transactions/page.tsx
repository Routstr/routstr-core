'use client';

import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import {
  RefreshCw,
  Search,
  ArrowDownLeft,
  ArrowUpRight,
  Copy,
  Check,
  Receipt,
} from 'lucide-react';
import { AdminService, type Transaction } from '@/lib/api/services/admin';
import { format } from 'date-fns';
import { toast } from 'sonner';

const STORAGE_KEY = 'routstr-transaction-filters';

export default function TransactionsPage() {
  const [search, setSearch] = useState('');
  const [type, setType] = useState<string>('all');
  const [status, setStatus] = useState<string>('all');
  const [copiedId, setCopiedId] = useState<string | null>(null);

  // Load filters from localStorage on mount
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        if (parsed.search) setSearch(parsed.search);
        if (parsed.type) setType(parsed.type);
        if (parsed.status) setStatus(parsed.status);
      } catch (e) {
        console.error('Failed to load filters from localStorage', e);
      }
    }
  }, []);

  // Save filters to localStorage whenever they change
  useEffect(() => {
    const filters = { search, type, status };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(filters));
  }, [search, type, status]);

  const { data, isLoading, refetch, isRefetching } = useQuery({
    queryKey: ['transactions', type, status, search],
    queryFn: () =>
      AdminService.getTransactions(
        type === 'all' ? undefined : type,
        status === 'all' ? undefined : status,
        search || undefined,
        100
      ),
  });

  const handleClearFilters = () => {
    setSearch('');
    setType('all');
    setStatus('all');
  };

  const copyToClipboard = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    toast.success('Copied to clipboard');
    setTimeout(() => setCopiedId(null), 2000);
  };

  const getStatusBadge = (tx: Transaction) => {
    if (tx.swept)
      return (
        <Badge
          variant='outline'
          className='border-orange-500/20 bg-orange-500/10 text-orange-500'
        >
          Swept
        </Badge>
      );
    if (tx.collected)
      return (
        <Badge
          variant='outline'
          className='border-green-500/20 bg-green-500/10 text-green-500'
        >
          Collected
        </Badge>
      );
    return (
      <Badge
        variant='outline'
        className='border-blue-500/20 bg-blue-500/10 text-blue-500'
      >
        Pending
      </Badge>
    );
  };

  const hasActiveFilters =
    type !== 'all' || status !== 'all' || Boolean(search);

  const activeFilterDescription = [
    type !== 'all' ? `type ${type === 'in' ? 'incoming' : 'outgoing'}` : null,
    status !== 'all' ? `status ${status}` : null,
    search ? `search "${search}"` : null,
  ]
    .filter(Boolean)
    .join(' • ');

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl overflow-x-hidden'>
      <div className='space-y-6'>
        <PageHeader
          title='X-Cashu Transactions'
          description='View all incoming and outgoing X-Cashu token transactions.'
          actions={
            <Button
              onClick={() => refetch()}
              variant='outline'
              size='sm'
              disabled={isRefetching}
            >
              <RefreshCw
                className={`mr-2 h-4 w-4 ${isRefetching ? 'animate-spin' : ''}`}
              />
              Refresh
            </Button>
          }
        />

        <Card className='mb-6'>
          <CardHeader>
            <CardTitle>Filters</CardTitle>
            <CardDescription>
              Filter transactions by type, status, or search text
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className='grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3'>
              <div className='space-y-2'>
                <Label htmlFor='search'>Search</Label>
                <div className='relative'>
                  <Search className='text-muted-foreground absolute top-2.5 left-2.5 h-4 w-4' />
                  <Input
                    id='search'
                    placeholder='Search by ID, token or request ID...'
                    className='pl-8'
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>
              </div>
              <div className='space-y-2'>
                <Label htmlFor='type'>Type</Label>
                <Select value={type} onValueChange={setType}>
                  <SelectTrigger>
                    <SelectValue placeholder='Type' />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value='all'>All Types</SelectItem>
                    <SelectItem value='in'>Incoming (Payments)</SelectItem>
                    <SelectItem value='out'>Outgoing (Refunds)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className='space-y-2'>
                <Label htmlFor='status'>Status</Label>
                <Select value={status} onValueChange={setStatus}>
                  <SelectTrigger>
                    <SelectValue placeholder='Status' />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value='all'>All Statuses</SelectItem>
                    <SelectItem value='pending'>Pending</SelectItem>
                    <SelectItem value='collected'>Collected</SelectItem>
                    <SelectItem value='swept'>Swept</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className='flex items-end sm:col-span-2 lg:col-span-1'>
                <Button
                  onClick={handleClearFilters}
                  variant='outline'
                  className='w-full'
                >
                  Clear Filters
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className='flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between'>
              <CardTitle>Transaction History</CardTitle>
              {data && (
                <Badge variant='secondary'>
                  {data.transactions.length} entries
                </Badge>
              )}
            </div>
            {hasActiveFilters && (
              <CardDescription>
                Showing transactions filtered by {activeFilterDescription}
              </CardDescription>
            )}
          </CardHeader>
          <CardContent className='overflow-hidden'>
            {isLoading ? (
              <div className='space-y-2'>
                {Array.from({ length: 8 }).map((_, index) => (
                  <Skeleton
                    key={`tx-loading-${index}`}
                    className='h-16 w-full rounded-lg'
                  />
                ))}
              </div>
            ) : data?.transactions && data.transactions.length > 0 ? (
              <ScrollArea className='h-[55svh] min-h-[420px] w-full sm:h-[600px]'>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Type</TableHead>
                      <TableHead>Amount</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Request ID</TableHead>
                      <TableHead>Mint</TableHead>
                      <TableHead>Date</TableHead>
                      <TableHead className='text-right'>Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.transactions.map((tx) => (
                      <TableRow key={tx.id}>
                        <TableCell>
                          <div className='flex items-center gap-2'>
                            {tx.type === 'in' ? (
                              <ArrowDownLeft className='h-4 w-4 text-green-500' />
                            ) : (
                              <ArrowUpRight className='h-4 w-4 text-blue-500' />
                            )}
                            <span className='capitalize'>{tx.type}</span>
                          </div>
                        </TableCell>
                        <TableCell className='font-mono'>
                          {tx.amount} {tx.unit}
                        </TableCell>
                        <TableCell>{getStatusBadge(tx)}</TableCell>
                        <TableCell>
                          {tx.request_id ? (
                            <div className='flex items-center gap-1 text-xs'>
                              <span className='max-w-[150px] truncate font-mono'>
                                {tx.request_id}
                              </span>
                              <Button
                                variant='ghost'
                                size='icon'
                                className='h-4 w-4'
                                onClick={() =>
                                  copyToClipboard(
                                    tx.request_id!,
                                    tx.id + '-req'
                                  )
                                }
                              >
                                {copiedId === tx.id + '-req' ? (
                                  <Check className='h-3 w-3' />
                                ) : (
                                  <Copy className='h-3 w-3' />
                                )}
                              </Button>
                            </div>
                          ) : (
                            <span className='text-muted-foreground text-xs'>
                              —
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          <div className='flex max-w-[150px] items-center gap-1 truncate text-xs'>
                            <span className='truncate'>{tx.mint_url}</span>
                          </div>
                        </TableCell>
                        <TableCell className='text-xs whitespace-nowrap'>
                          {format(tx.created_at * 1000, 'yyyy-MM-dd HH:mm:ss')}
                        </TableCell>
                        <TableCell className='text-right'>
                          <Button
                            variant='ghost'
                            size='icon'
                            className='h-8 w-8'
                            onClick={() =>
                              copyToClipboard(tx.token, tx.id + '-token')
                            }
                            title='Copy Token'
                          >
                            {copiedId === tx.id + '-token' ? (
                              <Check className='h-4 w-4' />
                            ) : (
                              <Copy className='h-4 w-4' />
                            )}
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ScrollArea>
            ) : (
              <Empty className='py-8'>
                <EmptyHeader>
                  <EmptyMedia variant='icon'>
                    <Receipt className='h-4 w-4' />
                  </EmptyMedia>
                  <EmptyTitle>No transactions found</EmptyTitle>
                  <EmptyDescription>
                    Try adjusting your filters or check back later.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
          </CardContent>
        </Card>
      </div>
    </AppPageShell>
  );
}
