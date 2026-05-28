'use client';

import { useState, useEffect } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { ScrollArea, ScrollBar } from '@/components/ui/scroll-area';
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
  Key,
  Zap,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import {
  AdminService,
  type Transaction,
  type LightningInvoice,
} from '@/lib/api/services/admin';
import { format } from 'date-fns';
import { toast } from 'sonner';

const STORAGE_KEY = 'routstr-transaction-filters';

function TransactionTable({
  transactions,
  copiedId,
  onCopy,
  getStatusBadge,
}: {
  transactions: Transaction[];
  copiedId: string | null;
  onCopy: (text: string, id: string) => void;
  getStatusBadge: (tx: Transaction) => React.ReactNode;
}) {
  if (transactions.length === 0) {
    return (
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
    );
  }

  return (
    <ScrollArea className='h-[55svh] min-h-[420px] w-full sm:h-[600px]'>
      <div className='min-w-[800px]'>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Type</TableHead>
              <TableHead>Amount</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>API Key</TableHead>
              <TableHead>Request ID</TableHead>
              <TableHead>Mint</TableHead>
              <TableHead>Date</TableHead>
              <TableHead className='text-right'>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {transactions.map((tx) => (
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
                  {tx.api_key_hashed_key ? (
                    <div className='flex items-center gap-1 text-xs'>
                      <span className='max-w-[120px] truncate font-mono'>
                        {tx.api_key_hashed_key.slice(0, 12)}...
                      </span>
                      <Button
                        variant='ghost'
                        size='icon'
                        className='h-4 w-4'
                        onClick={() =>
                          onCopy(tx.api_key_hashed_key!, tx.id + '-apikey')
                        }
                      >
                        {copiedId === tx.id + '-apikey' ? (
                          <Check className='h-3 w-3' />
                        ) : (
                          <Copy className='h-3 w-3' />
                        )}
                      </Button>
                    </div>
                  ) : (
                    <span className='text-muted-foreground text-xs'>—</span>
                  )}
                </TableCell>
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
                        onClick={() => onCopy(tx.request_id!, tx.id + '-req')}
                      >
                        {copiedId === tx.id + '-req' ? (
                          <Check className='h-3 w-3' />
                        ) : (
                          <Copy className='h-3 w-3' />
                        )}
                      </Button>
                    </div>
                  ) : (
                    <span className='text-muted-foreground text-xs'>—</span>
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
                    onClick={() => onCopy(tx.token, tx.id + '-token')}
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
      </div>
      <ScrollBar orientation='horizontal' />
    </ScrollArea>
  );
}

function LightningInvoiceTable({
  invoices,
  copiedId,
  onCopy,
}: {
  invoices: LightningInvoice[];
  copiedId: string | null;
  onCopy: (text: string, id: string) => void;
}) {
  if (invoices.length === 0) {
    return (
      <Empty className='py-8'>
        <EmptyHeader>
          <EmptyMedia variant='icon'>
            <Zap className='h-4 w-4' />
          </EmptyMedia>
          <EmptyTitle>No invoices found</EmptyTitle>
          <EmptyDescription>
            Lightning invoices created via /lightning/invoice will show here.
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  const statusBadge = (status: LightningInvoice['status']) => {
    if (status === 'paid')
      return (
        <Badge
          variant='outline'
          className='border-green-500/20 bg-green-500/10 text-green-500'
        >
          Paid
        </Badge>
      );
    if (status === 'expired')
      return (
        <Badge
          variant='outline'
          className='border-red-500/20 bg-red-500/10 text-red-500'
        >
          Expired
        </Badge>
      );
    if (status === 'cancelled')
      return (
        <Badge
          variant='outline'
          className='border-gray-500/20 bg-gray-500/10 text-gray-500'
        >
          Cancelled
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

  return (
    <ScrollArea className='h-[55svh] min-h-[420px] w-full sm:h-[600px]'>
      <div className='min-w-[900px]'>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Purpose</TableHead>
              <TableHead>Amount</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>API Key</TableHead>
              <TableHead>Payment Hash</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Paid</TableHead>
              <TableHead className='text-right'>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {invoices.map((inv) => (
              <TableRow key={inv.id}>
                <TableCell>
                  <span className='capitalize'>{inv.purpose}</span>
                </TableCell>
                <TableCell className='font-mono'>
                  {inv.amount_sats} sat
                </TableCell>
                <TableCell>{statusBadge(inv.status)}</TableCell>
                <TableCell>
                  {inv.api_key_hash ? (
                    <div className='flex items-center gap-1 text-xs'>
                      <span className='max-w-[120px] truncate font-mono'>
                        {inv.api_key_hash.slice(0, 12)}...
                      </span>
                      <Button
                        variant='ghost'
                        size='icon'
                        className='h-4 w-4'
                        onClick={() =>
                          onCopy(inv.api_key_hash!, inv.id + '-apikey')
                        }
                      >
                        {copiedId === inv.id + '-apikey' ? (
                          <Check className='h-3 w-3' />
                        ) : (
                          <Copy className='h-3 w-3' />
                        )}
                      </Button>
                    </div>
                  ) : (
                    <span className='text-muted-foreground text-xs'>—</span>
                  )}
                </TableCell>
                <TableCell>
                  <div className='flex items-center gap-1 text-xs'>
                    <span className='max-w-[140px] truncate font-mono'>
                      {inv.payment_hash.slice(0, 14)}...
                    </span>
                    <Button
                      variant='ghost'
                      size='icon'
                      className='h-4 w-4'
                      onClick={() => onCopy(inv.payment_hash, inv.id + '-hash')}
                    >
                      {copiedId === inv.id + '-hash' ? (
                        <Check className='h-3 w-3' />
                      ) : (
                        <Copy className='h-3 w-3' />
                      )}
                    </Button>
                  </div>
                </TableCell>
                <TableCell className='text-xs whitespace-nowrap'>
                  {format(inv.created_at * 1000, 'yyyy-MM-dd HH:mm:ss')}
                </TableCell>
                <TableCell className='text-xs whitespace-nowrap'>
                  {inv.paid_at
                    ? format(inv.paid_at * 1000, 'yyyy-MM-dd HH:mm:ss')
                    : '—'}
                </TableCell>
                <TableCell className='text-right'>
                  <Button
                    variant='ghost'
                    size='icon'
                    className='h-8 w-8'
                    onClick={() => onCopy(inv.bolt11, inv.id + '-bolt11')}
                    title='Copy BOLT11'
                  >
                    {copiedId === inv.id + '-bolt11' ? (
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
      </div>
      <ScrollBar orientation='horizontal' />
    </ScrollArea>
  );
}

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

  const PAGE_SIZE = 50;
  const [activeTab, setActiveTab] = useState<string>('x-cashu');
  const [xcashuPage, setXcashuPage] = useState(0);
  const [apikeyPage, setApikeyPage] = useState(0);
  const [lightningPage, setLightningPage] = useState(0);

  const typeParam = type === 'all' ? undefined : type;
  const statusParam = status === 'all' ? undefined : status;
  const searchParam = search || undefined;

  const xcashuQuery = useQuery({
    queryKey: [
      'transactions',
      'x-cashu',
      typeParam,
      statusParam,
      searchParam,
      xcashuPage,
    ],
    queryFn: () =>
      AdminService.getTransactions(
        typeParam,
        statusParam,
        searchParam,
        'x-cashu',
        PAGE_SIZE,
        xcashuPage * PAGE_SIZE
      ),
    placeholderData: keepPreviousData,
  });

  const apikeyQuery = useQuery({
    queryKey: [
      'transactions',
      'apikey',
      typeParam,
      statusParam,
      searchParam,
      apikeyPage,
    ],
    queryFn: () =>
      AdminService.getTransactions(
        typeParam,
        statusParam,
        searchParam,
        'apikey',
        PAGE_SIZE,
        apikeyPage * PAGE_SIZE
      ),
    placeholderData: keepPreviousData,
  });

  const LIGHTNING_STATUSES = ['pending', 'paid', 'expired', 'cancelled'];
  const lightningStatusParam = LIGHTNING_STATUSES.includes(status)
    ? status
    : undefined;

  const lightningQuery = useQuery({
    queryKey: [
      'lightning-invoices',
      lightningStatusParam,
      searchParam,
      lightningPage,
    ],
    queryFn: () =>
      AdminService.getLightningInvoices(
        lightningStatusParam,
        undefined,
        searchParam,
        PAGE_SIZE,
        lightningPage * PAGE_SIZE
      ),
    placeholderData: keepPreviousData,
    refetchInterval: 10000,
  });

  const handleClearFilters = () => {
    setSearch('');
    setType('all');
    setStatus('all');
    setXcashuPage(0);
    setApikeyPage(0);
    setLightningPage(0);
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

  // Reset pages when filters change
  useEffect(() => {
    setXcashuPage(0);
    setApikeyPage(0);
    setLightningPage(0);
  }, [type, status, search]);

  const isRefetching =
    xcashuQuery.isRefetching ||
    apikeyQuery.isRefetching ||
    lightningQuery.isRefetching;

  const renderCardContent = (
    query: typeof xcashuQuery,
    page: number,
    setPage: (p: number) => void
  ) => {
    if (query.isLoading) {
      return (
        <div className='space-y-2'>
          {Array.from({ length: 8 }).map((_, index) => (
            <Skeleton
              key={`tx-loading-${index}`}
              className='h-16 w-full rounded-lg'
            />
          ))}
        </div>
      );
    }

    const transactions = query.data?.transactions ?? [];
    const total = query.data?.total ?? 0;
    const totalPages = Math.ceil(total / PAGE_SIZE);

    return (
      <>
        {totalPages > 1 && (
          <div className='flex flex-col gap-2 border-b pb-3 sm:flex-row sm:items-center sm:justify-between'>
            <span className='text-muted-foreground text-xs sm:text-sm'>
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)}{' '}
              of {total}
            </span>
            <div className='flex items-center gap-2'>
              <Button
                variant='outline'
                size='sm'
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
              >
                <ChevronLeft className='h-4 w-4' />
                <span className='hidden sm:inline'>Previous</span>
              </Button>
              <span className='text-xs sm:text-sm'>
                {page + 1} / {totalPages}
              </span>
              <Button
                variant='outline'
                size='sm'
                disabled={page >= totalPages - 1}
                onClick={() => setPage(page + 1)}
              >
                <span className='hidden sm:inline'>Next</span>
                <ChevronRight className='h-4 w-4' />
              </Button>
            </div>
          </div>
        )}
        <TransactionTable
          transactions={transactions}
          copiedId={copiedId}
          onCopy={copyToClipboard}
          getStatusBadge={getStatusBadge}
        />
      </>
    );
  };

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl overflow-x-hidden'>
      <div className='space-y-6'>
        <PageHeader
          title='Cashu Transactions'
          description='View all incoming and outgoing Cashu token transactions.'
          actions={
            <Button
              onClick={() => {
                xcashuQuery.refetch();
                apikeyQuery.refetch();
                lightningQuery.refetch();
              }}
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
                    placeholder='Search by ID, token, request ID or key hash...'
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
                    <SelectItem value='paid'>Paid (Lightning)</SelectItem>
                    <SelectItem value='expired'>Expired (Lightning)</SelectItem>
                    <SelectItem value='cancelled'>
                      Cancelled (Lightning)
                    </SelectItem>
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

        <Tabs
          defaultValue='x-cashu'
          value={activeTab}
          onValueChange={setActiveTab}
        >
          <TabsList className='mb-4'>
            <TabsTrigger value='x-cashu' className='flex items-center gap-2'>
              <Zap className='h-4 w-4' />
              X-Cashu
              {xcashuQuery.data && (
                <Badge variant='secondary' className='ml-1'>
                  {xcashuQuery.data.total}
                </Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value='apikey' className='flex items-center gap-2'>
              <Key className='h-4 w-4' />
              API Key
              {apikeyQuery.data && (
                <Badge variant='secondary' className='ml-1'>
                  {apikeyQuery.data.total}
                </Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value='lightning' className='flex items-center gap-2'>
              <Zap className='h-4 w-4' />
              Lightning
              {lightningQuery.data && (
                <Badge variant='secondary' className='ml-1'>
                  {lightningQuery.data.total}
                </Badge>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent value='x-cashu'>
            <Card>
              <CardHeader>
                <div className='flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between'>
                  <CardTitle>X-Cashu Transaction History</CardTitle>
                  {hasActiveFilters && (
                    <CardDescription>
                      Filtered by {activeFilterDescription}
                    </CardDescription>
                  )}
                </div>
              </CardHeader>
              <CardContent className='overflow-hidden'>
                {renderCardContent(xcashuQuery, xcashuPage, setXcashuPage)}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value='apikey'>
            <Card>
              <CardHeader>
                <div className='flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between'>
                  <CardTitle>API Key Transaction History</CardTitle>
                  {hasActiveFilters && (
                    <CardDescription>
                      Filtered by {activeFilterDescription}
                    </CardDescription>
                  )}
                </div>
              </CardHeader>
              <CardContent className='overflow-hidden'>
                {renderCardContent(apikeyQuery, apikeyPage, setApikeyPage)}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value='lightning'>
            <Card>
              <CardHeader>
                <div className='flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between'>
                  <CardTitle>Lightning Invoice History</CardTitle>
                  <CardDescription>
                    Auto-refreshing every 10s. Paid invoices credit balance
                    automatically.
                  </CardDescription>
                </div>
              </CardHeader>
              <CardContent className='overflow-hidden'>
                {lightningQuery.isLoading ? (
                  <div className='space-y-2'>
                    {Array.from({ length: 8 }).map((_, index) => (
                      <Skeleton
                        key={`ln-loading-${index}`}
                        className='h-16 w-full rounded-lg'
                      />
                    ))}
                  </div>
                ) : (
                  <>
                    {(() => {
                      const total = lightningQuery.data?.total ?? 0;
                      const totalPages = Math.ceil(total / PAGE_SIZE);
                      if (totalPages <= 1) return null;
                      return (
                        <div className='flex flex-col gap-2 border-b pb-3 sm:flex-row sm:items-center sm:justify-between'>
                          <span className='text-muted-foreground text-xs sm:text-sm'>
                            {lightningPage * PAGE_SIZE + 1}–
                            {Math.min((lightningPage + 1) * PAGE_SIZE, total)}{' '}
                            of {total}
                          </span>
                          <div className='flex items-center gap-2'>
                            <Button
                              variant='outline'
                              size='sm'
                              disabled={lightningPage === 0}
                              onClick={() =>
                                setLightningPage(lightningPage - 1)
                              }
                            >
                              <ChevronLeft className='h-4 w-4' />
                              <span className='hidden sm:inline'>Previous</span>
                            </Button>
                            <span className='text-xs sm:text-sm'>
                              {lightningPage + 1} / {totalPages}
                            </span>
                            <Button
                              variant='outline'
                              size='sm'
                              disabled={lightningPage >= totalPages - 1}
                              onClick={() =>
                                setLightningPage(lightningPage + 1)
                              }
                            >
                              <span className='hidden sm:inline'>Next</span>
                              <ChevronRight className='h-4 w-4' />
                            </Button>
                          </div>
                        </div>
                      );
                    })()}
                    <LightningInvoiceTable
                      invoices={lightningQuery.data?.invoices ?? []}
                      copiedId={copiedId}
                      onCopy={copyToClipboard}
                    />
                  </>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </AppPageShell>
  );
}
